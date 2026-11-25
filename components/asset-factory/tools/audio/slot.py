#!/usr/bin/env python3
"""Reusable RP3 AUDIO slot: PCM16 validation, lossless master mux/decode,
independently pinned timing, signed silence, voice-route enforcement, and
priming-compensated H.264/yuv420p + AAC delivery encode.

Admits a caller-selected voice domain and verifies its bound media evidence.
"""

from __future__ import annotations

import argparse
from array import array
from fractions import Fraction
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
from math import ceil, isqrt, lcm
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave


SCHEMA_ID = "af.audio-mux/v1"
RECEIPT_SCHEMA = "rp3.audio-mux-receipt/v1"
TIMING_SCHEMA = "rp3.audio-timing/v1"
CONSENT_SCHEMA = "rp3.voice-consent/v1"
SILENCE_SCHEMA = "rp3.silence-disposition/v1"
PUBLICATION_SCHEMA = "rp3.publication-grant/v1"

LEGACY_VOICE_DOMAIN = "film_explainer_voiceover"
LEGACY_VOICE_PURPOSE = "tasklight_hackathon_explainer"
_VOICE_DOMAIN_RE = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z")
FORBIDDEN_PRODUCT_DOMAINS = frozenset({
    "tasklight_product_narrator",
    "firebase-hybrid",
    "firebase-ondevice",
    "firebase-cloud",
    "gemini-nano",
    "gemini-4-hydrogen",
    "rules-summary",
})

FIXTURE_ROUTES = frozenset({"local_fixture", "local_pilot"})
GENUINE_VOICE_PERMITTED_ROUTES = frozenset({"local_voiced_master", "operator_authorized_submission"})
SILENT_PERMITTED_ROUTES = frozenset({
    "local_fixture",
    "local_pilot",
    "local_silent_master",
    "local_caption_only_master",
    "operator_authorized_submission",
})

RC_VALUES = (0, 2, 3, 4, 5, 6)
STATUS_BY_RC = {
    0: "OK",
    2: "ERROR",
    3: "INCOMPLETE",
    4: "STALE",
    5: "FAIL",
    6: "BUDGET_EXCEEDED",
}
VERDICT_BY_RC = {
    0: "PASS",
    2: "ERROR",
    3: "CANNOT_SEE",
    4: "CANNOT_SEE",
    5: "FAIL",
    6: "CANNOT_SEE",
}

_LOCALE = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC"}
_ENV = {"PATH": "/usr/bin:/bin", "CUDA_VISIBLE_DEVICES": "", **_LOCALE}
_FFMPEG_BASE = ("-nostdin", "-v", "error", "-xerror", "-threads", "1")
_LOCAL_PROTO = ("-protocol_whitelist", "file")
_MAX_JSON_BYTES = 4 * 2**20
_MAX_AUDIO_BYTES = 64 * 2**20
_MAX_FRAME_BYTES = 64 * 2**20
_MAX_TIMELINE_BYTES = 32 * 2**20
_DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})\Z")
_SCOPE_LIMITS = (
    ("MemoryHigh", "memory.high", 6 * 2**30),
    ("MemoryMax", "memory.max", 8 * 2**30),
    ("MemorySwapMax", "memory.swap.max", 0),
)
_SCOPE_CHILD = "RP3_AUDIO_SCOPE_CHILD"
_SCOPE_ENTERED = "RP3_AUDIO_SCOPE_ENTERED"


class SlotRefusal(Exception):
    def __init__(self, rc: int, reason: str):
        self.rc = rc if rc in RC_VALUES else 2
        self.reason = str(reason)
        super().__init__(self.reason)


def _reject_float(_value: str):
    raise SlotRefusal(2, "non_integer_number")


def _unique_pairs(pairs):
    out = {}
    for key, val in pairs:
        if key in out:
            raise SlotRefusal(2, f"duplicate_json_key:{key}")
        out[key] = val
    return out


def canonical_json_bytes(obj: object) -> bytes:
    try:
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
        return text.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SlotRefusal(2, "canonical_json_failed") from exc


def sha256_file_hex(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as exc:
        raise SlotRefusal(3, f"file_unavailable:{path.name}") from exc
    return h.hexdigest()


def load_strict_json(path: Path, *, max_bytes: int = _MAX_JSON_BYTES, require_canonical: bool = False) -> tuple[object, bytes]:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise SlotRefusal(3, f"json_unavailable:{Path(path).name}") from exc
    if len(raw) > max_bytes:
        raise SlotRefusal(6, f"json_byte_budget:{Path(path).name}")
    try:
        obj = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except SlotRefusal:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise SlotRefusal(2, f"invalid_json:{Path(path).name}") from exc
    if require_canonical and raw != canonical_json_bytes(obj):
        raise SlotRefusal(2, f"noncanonical_json:{Path(path).name}")
    return obj, raw


def atomic_write_json(target: Path, obj: object) -> None:
    payload = canonical_json_bytes(obj)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=target.parent)
        staged = Path(tmp_name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, target)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def parse_sha256_pin(value: object, *, code: str = "invalid_digest_pin") -> str:
    if type(value) is not str:
        raise SlotRefusal(2, code)
    match = _DIGEST_RE.match(value)
    if not match:
        raise SlotRefusal(2, code)
    return match.group(1)


def resolve_local_path(base_dir: Path, raw_path: str, *, role: str) -> Path:
    if type(raw_path) is not str or not raw_path or "://" in raw_path:
        raise SlotRefusal(2, f"invalid_local_path:{role}")
    candidate = Path(raw_path)
    resolved = (candidate if candidate.is_absolute() else (base_dir / candidate)).resolve()
    if not resolved.exists():
        raise SlotRefusal(3, f"input_unavailable:{role}")
    return resolved


def run_local_cmd(argv: list[object], log_path: Path, timeout_s: int, *, failure_code: str = "external_process_failed") -> int:
    start = time.monotonic_ns()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log_stream:
        try:
            completed = subprocess.run(
                [str(a) for a in argv],
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                env=_ENV,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SlotRefusal(6, f"process_timeout:{failure_code}") from exc
        except OSError as exc:
            raise SlotRefusal(3, f"process_exec_failed:{failure_code}") from exc
    if completed.returncode < 0 or completed.returncode == 137:
        sig = -completed.returncode if completed.returncode < 0 else 9
        raise SlotRefusal(6, f"media_process_signal_killed:{failure_code}:{sig}")
    if completed.returncode != 0:
        raise SlotRefusal(2, f"{failure_code}:{completed.returncode}")
    return (time.monotonic_ns() - start) // 1_000_000


class _PortablePresentationMux:
    """Contract-identical stdlib fallback used only when outside an asset-factory tree."""

    @staticmethod
    def audio_pcm(path: Path | None, clock: dict | None) -> dict | None:
        if clock is None:
            if path is not None:
                raise SlotRefusal(4, "unexpected_narration")
            return None
        if path is None:
            raise SlotRefusal(3, "narration_unavailable")
        if path.stat().st_size > _MAX_AUDIO_BYTES:
            raise SlotRefusal(6, "audio_byte_budget")
        try:
            with wave.open(str(path), "rb") as source:
                rate, count, channels = source.getframerate(), source.getnframes(), source.getnchannels()
                if source.getsampwidth() != 2 or not 1 <= channels <= 2 or source.getcomptype() != "NONE":
                    raise SlotRefusal(2, "narration_pcm16_required")
                if rate != clock["sample_rate"] or count != clock["sample_count"] or count <= 0:
                    raise SlotRefusal(4, "narration_clock_mismatch")
                pcm = source.readframes(count + 1)
        except SlotRefusal:
            raise
        except (EOFError, wave.Error, OSError) as exc:
            raise SlotRefusal(2, "invalid_narration_wav") from exc
        if len(pcm) != count * channels * 2:
            raise SlotRefusal(4, "narration_pcm_size_mismatch")
        return {
            "sample_rate": rate,
            "sample_count": count,
            "channels": channels,
            "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
        }

    @staticmethod
    def validate_timing(schedule: list[dict], fps: list[int] | tuple[int, int], clock: dict | None):
        fps_q = Fraction(*fps)
        if not schedule or fps_q <= 0:
            raise SlotRefusal(3, "presentation_mux_timing_unqualified")
        for index, slot in enumerate(schedule):
            times = tuple(Fraction(*pair) for pair in slot["time"])
            if slot["frame"] != schedule[0]["frame"] + index or times != (index / fps_q, (index + 1) / fps_q):
                raise SlotRefusal(3, "presentation_mux_timing_unqualified")
            expected = [ceil(t * clock["sample_rate"]) for t in times] if clock else None
            if slot["samples"] != expected:
                raise SlotRefusal(4, "presentation_audio_schedule_mismatch")
        if clock and schedule[-1]["samples"][1] != clock["sample_count"]:
            raise SlotRefusal(4, "presentation_audio_schedule_mismatch")
        movie_scale = lcm(fps_q.numerator, clock["sample_rate"] if clock else 1)
        if movie_scale > 2**31 - 1:
            raise SlotRefusal(3, "unsupported_mux_timebase")
        return fps_q, movie_scale

    @staticmethod
    def mux(root: Path, film: Path, *, schedule: list[dict], fps_q: Fraction, movie_scale: int,
            audio: Path | None, pcm: dict | None, ffmpeg: Path, timeout: int, scratch: Path) -> None:
        cmd = [
            ffmpeg, *_FFMPEG_BASE, *_LOCAL_PROTO,
            "-f", "image2", "-framerate", str(fps_q),
            "-start_number", schedule[0]["frame"], "-start_number_range", 1,
            "-i", root / "low/%06d/denoised.png",
        ]
        if pcm:
            cmd += [*_LOCAL_PROTO, "-f", "wav", "-i", audio, "-map", "1:a:0", "-c:a", "pcm_s16le"]
        cmd += [
            "-map", "0:v:0", "-c:v", "png", "-pix_fmt", "rgb24", "-threads:v", 1,
            "-fps_mode", "passthrough", "-video_track_timescale", fps_q.numerator,
            "-movie_timescale", movie_scale, "-map_metadata", "-1", "-map_chapters", "-1",
            "-f", "mov", "-n", film,
        ]
        run_local_cmd(cmd, scratch / "mux.log", timeout, failure_code="master_mux_failed")

def _verify_master(film: Path, scratch: Path, *, schedule: list[dict], width: int, height: int,
                   frame_digests: dict[int, str], pcm: dict | None, ffmpeg: Path,
                   ffprobe: Path, timeout: int) -> dict:
    """Hash each decoded RGB frame in FFmpeg and keep only the hash and clock records."""
    before = sha256_file_hex(film)
    probe_path = scratch / "master-timeline.json"
    run_local_cmd(
        [
            ffprobe, "-v", "error", "-threads", 1, *_LOCAL_PROTO, "-f", "mov", "-i", film,
            "-show_streams", "-show_frames", "-show_entries",
            "stream=index,codec_type,codec_name,pix_fmt,width,height,time_base,start_pts,duration_ts,sample_rate,channels:"
            "frame=stream_index,media_type,pts,duration,pkt_duration,width,height,nb_samples,channels",
            "-of", "json",
        ],
        probe_path, timeout, failure_code="master_probe_failed",
    )
    report, _ = load_strict_json(probe_path, max_bytes=_MAX_TIMELINE_BYTES)
    timeline = _verify_mov_timeline(report, schedule, width, height, pcm)
    src = [ffmpeg, *_FFMPEG_BASE, *_LOCAL_PROTO, "-f", "mov", "-i", film]
    hashes = scratch / "master-framehash.txt"
    run_local_cmd(
        [*src, "-map", "0:v:0", "-fps_mode", "passthrough", "-pix_fmt", "rgb24",
         "-c:v", "rawvideo", "-threads:v", 1, "-frames:v", len(schedule) + 1,
         "-f", "framehash", "-hash", "sha256", "-n", hashes],
        scratch / "master-framehash.log", timeout, failure_code="master_decode_video_failed",
    )
    frame_bytes = width * height * 3
    frames = iter(schedule)
    with hashes.open("rt", encoding="ascii") as records:
        for line in records:
            if line.startswith("#") or not line.strip():
                continue
            slot = next(frames, None)
            if slot is None:
                raise SlotRefusal(4, "decoded_frame_size_or_count_mismatch")
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 6 or fields[0] != "0" or fields[4] != str(frame_bytes):
                raise SlotRefusal(4, "decoded_frame_size_or_count_mismatch")
            if fields[5] != frame_digests[slot["frame"]]:
                raise SlotRefusal(4, f'decoded_frame_payload_mismatch:{slot["frame"]}')
        if next(frames, None) is not None:
            raise SlotRefusal(4, "decoded_frame_size_or_count_mismatch")

    if pcm:
        # The independent sample timeline above proves count, continuity and sample rate.
        audio_hash = scratch / "master-audiohash.txt"
        run_local_cmd(
            [*src, "-map", "0:a:0", "-c:a", "pcm_s16le", "-f", "hash",
             "-hash", "sha256", "-n", audio_hash],
            scratch / "master-audiohash.log", timeout, failure_code="master_decode_audio_failed",
        )
        if audio_hash.read_bytes() != f'SHA256={pcm["pcm_sha256"]}\n'.encode("ascii"):
            raise SlotRefusal(4, "decoded_audio_payload_mismatch")
    if sha256_file_hex(film) != before:
        raise SlotRefusal(4, "film_changed_during_decode")
    return {**timeline, "film_sha256": before, "pcm": pcm}


def _frame_duration_ticks(row: dict) -> int:
    vals = []
    for field in ("duration", "pkt_duration"):
        if field in row:
            v = row[field]
            if type(v) not in (int, str) or (type(v) is str and not v.isdecimal()):
                raise ValueError("invalid frame duration")
            vals.append(int(v))
    if not vals or min(vals) <= 0 or len(set(vals)) != 1:
        raise ValueError("missing or inconsistent frame duration")
    return vals[0]


def _verify_mov_timeline(report: dict, schedule: list[dict], width: int, height: int, pcm: dict | None) -> dict:
    try:
        streams = report["streams"]
        video_streams = [s for s in streams if s["codec_type"] == "video"]
        audio_streams = [s for s in streams if s["codec_type"] == "audio"]
        if len(video_streams) != 1 or len(audio_streams) != bool(pcm) or len(streams) != 1 + bool(pcm):
            raise SlotRefusal(4, "decoded_stream_membership_mismatch")
        video = video_streams[0]
        if (video["codec_name"], video["pix_fmt"], video["width"], video["height"]) != ("png", "rgb24", width, height):
            raise SlotRefusal(4, "decoded_video_format_mismatch")
        v_base = Fraction(video["time_base"])
        v_start, v_dur = int(video["start_pts"]), int(video["duration_ts"])
        if v_base <= 0 or v_start != 0 or v_dur <= 0:
            raise SlotRefusal(4, "decoded_stream_clock_mismatch")
        if v_base * v_dur != Fraction(*schedule[-1]["time"][1]):
            raise SlotRefusal(4, "decoded_video_duration_mismatch")
        frames = report["frames"]
        v_frames = [r for r in frames if r["stream_index"] == video["index"]]
        if len(v_frames) != len(schedule):
            raise SlotRefusal(4, "decoded_frame_count_mismatch")
        v_timeline = []
        for row, slot in zip(v_frames, schedule):
            start_q, end_q = (Fraction(*p) for p in slot["time"])
            pts = int(row["pts"])
            ticks = _frame_duration_ticks(row)
            if (row["media_type"] != "video" or row["width"] != width or row["height"] != height
                    or v_base * pts != start_q or v_base * ticks != end_q - start_q):
                raise SlotRefusal(4, "decoded_video_pts_mismatch")
            v_timeline.append({"frame": slot["frame"], "pts": pts, "duration": ticks})
        a_timeline = []
        a_base_str = None
        if pcm:
            audio = audio_streams[0]
            rate = pcm["sample_rate"]
            if (audio["codec_name"], int(audio["sample_rate"]), audio["channels"]) != ("pcm_s16le", rate, pcm["channels"]):
                raise SlotRefusal(4, "decoded_audio_format_mismatch")
            a_base = Fraction(audio["time_base"])
            a_start, a_dur = int(audio["start_pts"]), int(audio["duration_ts"])
            if a_base <= 0 or a_start != 0 or a_dur <= 0:
                raise SlotRefusal(4, "decoded_stream_clock_mismatch")
            if a_base * a_dur * rate != pcm["sample_count"]:
                raise SlotRefusal(4, "decoded_audio_duration_mismatch")
            offset = 0
            for row in frames:
                if row["stream_index"] != audio["index"]:
                    continue
                pts, samples, ticks = int(row["pts"]), int(row["nb_samples"]), _frame_duration_ticks(row)
                if (row["media_type"] != "audio" or row["channels"] != pcm["channels"] or samples <= 0
                        or a_base * pts * rate != offset or a_base * ticks * rate != samples):
                    raise SlotRefusal(4, "decoded_audio_pts_mismatch")
                a_timeline.append({"pts": pts, "start_sample": offset, "sample_count": samples})
                offset += samples
            if offset != pcm["sample_count"]:
                raise SlotRefusal(4, "decoded_sample_count_mismatch")
            a_base_str = str(a_base)
        if len(frames) != len(v_timeline) + len(a_timeline):
            raise SlotRefusal(4, "unexpected_decoded_frame")
        return {
            "video_time_base": str(v_base),
            "video": v_timeline,
            "audio_time_base": a_base_str,
            "audio": a_timeline,
        }
    except SlotRefusal:
        raise
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise SlotRefusal(4, "decoded_pts_evidence_invalid") from exc


def load_mux_backend(factory_root: Path | None = None, *, require_factory_mux: bool = False):
    candidates: list[Path] = []
    if factory_root is not None:
        candidates.append(Path(factory_root).resolve())
    env_root = os.environ.get("ASSET_FACTORY_ROOT")
    if env_root:
        candidates.append(Path(env_root).resolve())
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "asset_factory" / "core.py").is_file() and (parent / "tools" / "render" / "presentation_mux.py").is_file():
            candidates.append(parent)
            break
    for root in candidates:
        root_str = str(root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
    try:
        from asset_factory import core as af_core
        from tools.render import presentation_mux as af_pmux
        pmux_path = Path(af_pmux.__file__).resolve()
        return af_core, af_pmux, "asset_factory.tools.render.presentation_mux", sha256_file_hex(pmux_path)
    except Exception as exc:
        if require_factory_mux:
            raise SlotRefusal(3, "asset_factory_presentation_mux_unavailable") from exc
        return None, _PortablePresentationMux, "portable_presentation_mux", sha256_file_hex(here)


def validate_against_contract(payload: dict, contract_path: Path, af_core=None) -> None:
    schema, _ = load_strict_json(contract_path)
    if af_core is not None:
        try:
            af_core.validate(payload, schema)
            return
        except Exception as exc:
            code = getattr(exc, "code", "contract_validation_failed")
            raise SlotRefusal(2, f"contract_invalid:{code}") from exc
    try:
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
        errors = list(Draft202012Validator(schema).iter_errors(payload))
        if errors:
            raise SlotRefusal(2, f"contract_invalid:{errors[0].validator}")
    except ImportError as exc:
        raise SlotRefusal(3, "contract_validator_unavailable") from exc


def _call_pmux(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except SlotRefusal:
        raise
    except Exception as exc:
        if hasattr(exc, "rc") and hasattr(exc, "reason"):
            raise SlotRefusal(int(exc.rc), str(exc.reason)) from exc
        if hasattr(exc, "rc") and hasattr(exc, "code"):
            raise SlotRefusal(int(exc.rc), str(exc.code)) from exc
        raise SlotRefusal(2, f"mux_error:{type(exc).__name__}") from exc


def _pcm_peaks_and_mono(pcm_bytes: bytes, channels: int) -> tuple[array, array]:
    if len(pcm_bytes) % (2 * channels) != 0:
        raise SlotRefusal(2, "invalid_pcm_byte_alignment")
    samples = array("h")
    samples.frombytes(pcm_bytes)
    if sys.byteorder != "little":
        samples.byteswap()
    peaks = array("H")
    mono = array("h")
    if channels == 1:
        peaks.extend(abs(v) for v in samples)
        mono.extend(samples)
    else:
        for idx in range(0, len(samples), channels):
            left = samples[idx]
            right = samples[idx + 1]
            peaks.append(max(abs(left), abs(right)))
            mono.append((left + right) // 2)
    return peaks, mono


def _activity_intervals(peaks: array, threshold: int, bridge_samples: int) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    start = last = None
    for idx, peak in enumerate(peaks):
        if peak <= threshold:
            continue
        if last is None or idx - last > bridge_samples + 1:
            if last is not None:
                intervals.append((start, last + 1))
            start = idx
        last = idx
    if last is not None:
        intervals.append((start, last + 1))
    return intervals


def _pearson_correlation_ppm(ref_mono: array, cand_mono: array) -> int:
    if len(ref_mono) != len(cand_mono) or not ref_mono:
        return 0
    n = len(ref_mono)
    sum_x = sum(ref_mono)
    sum_y = sum(cand_mono)
    sum_xx = sum(x * x for x in ref_mono)
    sum_yy = sum(y * y for y in cand_mono)
    sum_xy = sum(x * y for x, y in zip(ref_mono, cand_mono))
    num = n * sum_xy - sum_x * sum_y
    den_x = n * sum_xx - sum_x * sum_x
    den_y = n * sum_yy - sum_y * sum_y
    if den_x <= 0 or den_y <= 0:
        return 1_000_000 if den_x == den_y else 0
    if num <= 0:
        return 0
    den = isqrt(den_x * den_y)
    if den <= 0:
        return 0
    return min(1_000_000, (num * 1_000_000) // den)


def _rgb_bytes_from_png(path: Path) -> tuple[int, int, bytes]:
    from PIL import Image
    with Image.open(path) as img:
        w, h = img.size
        if w * h * 3 > _MAX_FRAME_BYTES:
            raise SlotRefusal(6, "picture_frame_byte_budget")
        return w, h, img.convert("RGB").tobytes()


def _build_schedule(picture: dict, clock: dict | None) -> list[dict]:
    start = picture["start_frame"]
    end = picture["end_frame"]
    fps_pair = picture["fps"]
    if (type(fps_pair) is not list or len(fps_pair) != 2
            or any(type(v) is not int for v in fps_pair) or fps_pair[0] <= 0 or fps_pair[1] <= 0):
        raise SlotRefusal(2, "invalid_fps_rational")
    fps = Fraction(fps_pair[0], fps_pair[1])
    if [fps.numerator, fps.denominator] != fps_pair:
        raise SlotRefusal(2, "unreduced_fps_rational")
    if not (0 <= start < end and end - start <= 4320):
        raise SlotRefusal(2, "invalid_frame_range")

    cuts = picture["camera_cuts"]
    if not cuts or cuts[0]["frame"] != start:
        raise SlotRefusal(4, "initial_camera_cut_missing")
    cameras: dict[int, str] = {}
    prev_frame = start - 1
    for cut in cuts:
        cf = cut["frame"]
        at = cut["at"]
        expected_at = ( cf - start ) / fps
        if not (prev_frame < cf < end) or at != [expected_at.numerator, expected_at.denominator] or not cut["camera"]:
            raise SlotRefusal(4, "camera_cut_timebase_mismatch")
        cameras[cf] = cut["camera"]
        prev_frame = cf

    boundaries = [(offset / fps) for offset in range(end - start + 1)]
    samples = None
    if clock is not None:
        samples = [ceil(t * clock["sample_rate"]) for t in boundaries]
        if samples[-1] != clock["sample_count"]:
            raise SlotRefusal(4, "audio_sample_count_mismatch")

    schedule = []
    active_cam = cameras[start]
    for idx, frame in enumerate(range(start, end)):
        active_cam = cameras.get(frame, active_cam)
        t0, t1 = boundaries[idx], boundaries[idx + 1]
        schedule.append({
            "frame": frame,
            "camera": active_cam,
            "time": [[t0.numerator, t0.denominator], [t1.numerator, t1.denominator]],
            "samples": [samples[idx], samples[idx + 1]] if samples is not None else None,
        })
    return schedule


def _stage_picture_frames(picture: dict, base_dir: Path, scratch: Path, ffmpeg: Path,
                          timeout_s: int, *, stage: bool = True) -> tuple[Path, dict[int, str]]:
    kind = picture["kind"]
    width, height = picture["width"], picture["height"]
    start, end = picture["start_frame"], picture["end_frame"]
    if width <= 0 or height <= 0 or width % 2 != 0 or height % 2 != 0:
        raise SlotRefusal(2, "picture_dimensions_require_even_raster")
    if width * height * 3 > _MAX_FRAME_BYTES:
        raise SlotRefusal(6, "picture_frame_byte_budget")

    src_path = resolve_local_path(base_dir, picture["path"], role="picture")
    if picture["sha256"] is not None:
        expected_pic_sha = parse_sha256_pin(picture["sha256"], code="invalid_picture_pin")
        if not src_path.is_file() or sha256_file_hex(src_path) != expected_pic_sha:
            raise SlotRefusal(4, "picture_source_digest_mismatch")

    expected_hashes = {row["frame"]: parse_sha256_pin(row["sha256"], code="invalid_frame_pin") for row in picture["frame_hashes"]}
    expected_frames = list(range(start, end))
    if len(picture["frame_hashes"]) != len(expected_frames) or sorted(expected_hashes) != expected_frames:
        raise SlotRefusal(4, "picture_frame_hashes_incomplete")

    stage_root = scratch / "staged_frames"
    if kind == "lossless_mov":
        if picture["sha256"] is None or not src_path.is_file():
            raise SlotRefusal(3, "lossless_mov_pin_required")
        extract_dir = scratch / "extracted_png"
        extract_dir.mkdir(parents=True, exist_ok=True)
        run_local_cmd(
            [
                ffmpeg, *_FFMPEG_BASE, *_LOCAL_PROTO, "-i", src_path,
                "-map", "0:v:0", "-fps_mode", "passthrough", "-pix_fmt", "rgb24",
                "-threads:v", 1, "-frames:v", len(expected_frames) + 1,
                "-start_number", start, extract_dir / "%06d.png",
            ],
            scratch / "extract_mov.log",
            timeout_s,
            failure_code="picture_mov_extract_failed",
        )

        if (extract_dir / f"{end:06d}.png").exists():
            raise SlotRefusal(4, "picture_source_frame_count_mismatch")

    frame_digests: dict[int, str] = {}
    for frame in expected_frames:
        if kind == "f5_frames_root":
            frame_png = src_path / "low" / f"{frame:06d}" / "denoised.png"
        elif kind == "png_sequence_dir":
            frame_png = src_path / f"{frame:06d}.png"
        else:
            frame_png = scratch / "extracted_png" / f"{frame:06d}.png"
        if not frame_png.is_file():
            raise SlotRefusal(3, f"picture_frame_missing:{frame}")
        actual_file_sha = sha256_file_hex(frame_png)
        w, h, rgb_bytes = _rgb_bytes_from_png(frame_png)
        if (w, h) != (width, height):
            raise SlotRefusal(4, f"picture_frame_dimension_mismatch:{frame}")
        rgb_sha = hashlib.sha256(rgb_bytes).hexdigest()
        pin = expected_hashes[frame]
        if pin not in (actual_file_sha, rgb_sha):
            raise SlotRefusal(4, f"picture_frame_digest_mismatch:{frame}")
        frame_digests[frame] = rgb_sha
        if stage:
            dst_dir = stage_root / "low" / f"{frame:06d}"
            dst_dir.mkdir(parents=True, exist_ok=True)
            destination = dst_dir / "denoised.png"
            try:
                os.link(frame_png.resolve(), destination)
            except OSError:
                shutil.copyfile(frame_png, destination)

    return stage_root, frame_digests


def _voice_domain(request: dict) -> str:
    domain = request.get("voice_domain")
    if not isinstance(domain, str) or _VOICE_DOMAIN_RE.fullmatch(domain) is None:
        raise SlotRefusal(2, "invalid_voice_domain")
    if domain in FORBIDDEN_PRODUCT_DOMAINS:
        raise SlotRefusal(5, "voice_role_conflated_with_product_narrator")
    if "voice_purpose" in request:
        purpose = request["voice_purpose"]
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 256:
            raise SlotRefusal(2, "invalid_voice_purpose")
    return domain


def _validate_voice_route_and_inputs(request: dict, base_dir: Path, pmux) -> tuple[Path | None, dict | None, dict | None, dict | None, dict | None]:
    domain = request["voice_domain"]

    mode = request["mode"]
    route = request["route"]
    scope = request["scope"]
    narration = request["narration"]
    silence_ref = request["silence_disposition"]

    if mode == "silent":
        if narration is not None:
            _call_pmux(pmux.audio_pcm, Path("/unexpected"), None)
            raise SlotRefusal(4, "unexpected_narration")
        if silence_ref is None:
            raise SlotRefusal(3, "silence_disposition_missing")
        if route not in SILENT_PERMITTED_ROUTES:
            raise SlotRefusal(5, "silent_route_unpermitted")
        if request["delivery"]["audio_codec"] != "none":
            raise SlotRefusal(2, "silent_delivery_must_exclude_audio_track")
        disp_path = resolve_local_path(base_dir, silence_ref["path"], role="silence_disposition")
        expected_disp_sha = parse_sha256_pin(silence_ref["sha256"], code="invalid_silence_pin")
        disp_obj, disp_raw = load_strict_json(disp_path, require_canonical=True)
        if hashlib.sha256(disp_raw).hexdigest() != expected_disp_sha:
            raise SlotRefusal(4, "silence_disposition_digest_mismatch")
        if (type(disp_obj) is not dict or disp_obj.get("schema") != SILENCE_SCHEMA
                or disp_obj.get("voice_domain") != domain
                or disp_obj.get("disposition") not in ("signed_silence", "caption_only_cutoff", "caption_only_standby")
                or (disp_obj.get("disposition") == "caption_only_standby"
                    and disp_obj.get("publication_authorized") is not False)
                or disp_obj.get("fps") != request["picture"]["fps"]
                or disp_obj.get("start_frame") != request["picture"]["start_frame"]
                or disp_obj.get("end_frame") != request["picture"]["end_frame"]):
            raise SlotRefusal(4, "silence_disposition_contract_mismatch")
        if route == "operator_authorized_submission" and disp_obj.get("publication_authorized") is not True:
            raise SlotRefusal(5, "silent_publication_not_authorized")
        _call_pmux(pmux.audio_pcm, None, None)
        return None, None, None, disp_obj, None

    if narration is None or silence_ref is not None:
        raise SlotRefusal(3, "narration_configuration_incomplete")
    if request["delivery"]["enabled"] and request["delivery"]["audio_codec"] != "aac":
        raise SlotRefusal(2, "voiced_delivery_requires_aac")

    source_kind = narration["source_kind"]
    if source_kind in FORBIDDEN_PRODUCT_DOMAINS:
        raise SlotRefusal(5, "voice_role_conflated_with_product_narrator")

    # Authenticate independently pinned timing first before inspecting timing contents.
    timing_path = resolve_local_path(base_dir, narration["timing_path"], role="timing")
    expected_timing_sha = parse_sha256_pin(narration["timing_sha256"], code="invalid_timing_pin")
    timing_obj, timing_raw = load_strict_json(timing_path, require_canonical=True)
    if hashlib.sha256(timing_raw).hexdigest() != expected_timing_sha:
        raise SlotRefusal(4, "timing_digest_mismatch")
    if type(timing_obj) is not dict or timing_obj.get("schema") != TIMING_SCHEMA:
        raise SlotRefusal(2, "invalid_timing_manifest")
    timing_domain = timing_obj.get("voice_domain")
    if isinstance(timing_domain, str) and timing_domain in FORBIDDEN_PRODUCT_DOMAINS:
        raise SlotRefusal(5, "voice_role_conflated_with_product_narrator")
    if timing_domain != domain:
        raise SlotRefusal(4, "timing_voice_domain_mismatch")
    _validate_timing_fields(timing_obj)

    req_script_sha = parse_sha256_pin(narration["script_sha256"], code="invalid_script_pin")
    timing_script_sha = parse_sha256_pin(timing_obj.get("script_sha256"), code="invalid_timing_script_pin")
    if req_script_sha != timing_script_sha:
        raise SlotRefusal(4, "timing_script_digest_mismatch")

    pic = request["picture"]
    if (timing_obj.get("fps") != pic["fps"]
            or timing_obj.get("start_frame") != pic["start_frame"]
            or timing_obj.get("end_frame") != pic["end_frame"]):
        raise SlotRefusal(4, "timing_picture_window_mismatch")

    wav_path = resolve_local_path(base_dir, narration["wav_path"], role="narration_wav")
    expected_wav_sha = parse_sha256_pin(narration["wav_sha256"], code="invalid_wav_pin")
    timing_wav_sha = parse_sha256_pin(timing_obj.get("wav_sha256"), code="invalid_timing_wav_pin")
    expected_pcm_sha = parse_sha256_pin(timing_obj.get("pcm_sha256"), code="invalid_timing_pcm_pin")

    clock = {
        "sample_rate": timing_obj["sample_rate"],
        "sample_count": timing_obj["sample_count"],
    }
    pcm = _call_pmux(pmux.audio_pcm, wav_path, clock)
    if pcm["channels"] != timing_obj.get("channels"):
        raise SlotRefusal(4, "narration_channel_count_mismatch")

    actual_wav_sha = sha256_file_hex(wav_path)
    if actual_wav_sha != expected_wav_sha or actual_wav_sha != timing_wav_sha or pcm["pcm_sha256"] != expected_pcm_sha:
        raise SlotRefusal(4, "pcm_payload_digest_mismatch")

    if source_kind != "synthetic_fixture":
        policy_path = Path(__file__).resolve().parents[1] / "explainer_factory/package/voice_policy.py"
        spec = spec_from_file_location("rp3_voice_policy", policy_path)
        policy = module_from_spec(spec)
        spec.loader.exec_module(policy)
        if actual_wav_sha in policy.KNOWN_FIXTURE_WAV_DIGESTS or pcm["pcm_sha256"] in policy.KNOWN_FIXTURE_PCM_DIGESTS:
            raise SlotRefusal(5, "substituted_voice_source")

    # Enforce strict route separation between synthetic fixtures and genuine operator voice.
    consent_obj = None
    if source_kind == "synthetic_fixture":
        if scope != "build-fixture" or route not in FIXTURE_ROUTES:
            raise SlotRefusal(5, "fixture_audio_barred_from_production_route")
    elif source_kind == "genuine_operator_voice":
        if route not in GENUINE_VOICE_PERMITTED_ROUTES:
            raise SlotRefusal(5, "genuine_voice_route_unpermitted")
        if narration["consent_path"] is None or narration["consent_sha256"] is None:
            raise SlotRefusal(5, "genuine_voice_consent_missing")
        consent_path = resolve_local_path(base_dir, narration["consent_path"], role="voice_consent")
        expected_consent_sha = parse_sha256_pin(narration["consent_sha256"], code="invalid_consent_pin")
        consent_obj, consent_raw = load_strict_json(consent_path, require_canonical=True)
        if hashlib.sha256(consent_raw).hexdigest() != expected_consent_sha:
            raise SlotRefusal(4, "voice_consent_digest_mismatch")
        if type(consent_obj) is not dict or consent_obj.get("schema") != CONSENT_SCHEMA:
            raise SlotRefusal(5, "invalid_voice_consent")
        purpose = request.get("voice_purpose")
        legacy_purpose = purpose is None and domain == LEGACY_VOICE_DOMAIN
        if legacy_purpose:
            purpose = LEGACY_VOICE_PURPOSE
        if consent_obj.get("historical_basis") == "f7_silent_approval":
            raise SlotRefusal(5, "historical_f7_approval_insufficient")
        if not purpose or consent_obj.get("purpose") != purpose:
            reason = "historical_f7_approval_insufficient" if legacy_purpose else "voice_consent_purpose_mismatch"
            raise SlotRefusal(5, reason)
        if (consent_obj.get("subject_authority") != "operator"
                or consent_obj.get("assent") is not True
                or consent_obj.get("voice_domain") != domain
                or consent_obj.get("provider_egress_allowed") is not False):
            raise SlotRefusal(5, "voice_consent_unpermitted")
        permitted = consent_obj.get("permitted_routes")
        if (type(permitted) is not list or not permitted
                or any(r not in GENUINE_VOICE_PERMITTED_ROUTES for r in permitted)
                or route not in permitted):
            raise SlotRefusal(5, "genuine_voice_route_unpermitted")
        if (parse_sha256_pin(consent_obj.get("wav_sha256"), code="invalid_consent_wav_pin") != actual_wav_sha
                or parse_sha256_pin(consent_obj.get("pcm_sha256"), code="invalid_consent_pcm_pin") != pcm["pcm_sha256"]
                or parse_sha256_pin(consent_obj.get("script_sha256"), code="invalid_consent_script_pin") != req_script_sha):
            raise SlotRefusal(4, "voice_consent_digest_mismatch")
        if route == "operator_authorized_submission":
            if narration["publication_grant_path"] is None or narration["publication_grant_sha256"] is None:
                raise SlotRefusal(5, "genuine_voice_route_unpermitted")
            pub_path = resolve_local_path(base_dir, narration["publication_grant_path"], role="publication_grant")
            expected_pub_sha = parse_sha256_pin(narration["publication_grant_sha256"], code="invalid_publication_pin")
            pub_obj, pub_raw = load_strict_json(pub_path, require_canonical=True)
            if hashlib.sha256(pub_raw).hexdigest() != expected_pub_sha:
                raise SlotRefusal(4, "publication_grant_digest_mismatch")
            if (type(pub_obj) is not dict or pub_obj.get("schema") != PUBLICATION_SCHEMA
                    or pub_obj.get("authority") != "operator" or pub_obj.get("approved") is not True
                    or parse_sha256_pin(pub_obj.get("wav_sha256")) != actual_wav_sha
                    or (not legacy_purpose and (
                        pub_obj.get("voice_domain") != domain or pub_obj.get("purpose") != purpose
                        or pub_obj.get("script_sha256") != narration["script_sha256"]))):
                raise SlotRefusal(5, "genuine_voice_route_unpermitted")
    else:
        raise SlotRefusal(2, "unknown_narration_source_kind")

    return wav_path, pcm, timing_obj, None, consent_obj


def _validate_timing_fields(timing: dict) -> None:
    required = (
        "sample_rate", "sample_count", "channels", "silence_peak_pcm", "bridge_samples",
        "onset_tolerance_samples", "delivery_onset_tolerance_samples",
    )
    optional = ("tail_tolerance_samples", "delivery_tail_tolerance_samples")
    for field in (*required, *(name for name in optional if name in timing)):
        if type(timing.get(field)) is not int or timing[field] < 0:
            raise SlotRefusal(2, f"invalid_timing_field:{field}")
    if timing["sample_rate"] <= 0 or timing["sample_count"] <= 0 or not 1 <= timing["channels"] <= 2:
        raise SlotRefusal(2, "invalid_timing_clock")
    cues = timing.get("cues")
    if not isinstance(cues, list) or not cues or not isinstance(timing.get("silences"), list):
        raise SlotRefusal(2, "invalid_timing_cues")
    for cue in cues:
        if not isinstance(cue, dict) or any(type(cue.get(key)) is not int for key in (
            "start_frame", "end_frame", "expected_onset_sample", "expected_end_sample",
        )):
            raise SlotRefusal(2, "invalid_timing_cue")


def _verify_pcm_cues_and_silences(pcm_bytes: bytes, channels: int, sample_count: int, timing_obj: dict, *,
                                  tolerance_samples: int, onset_error_code: str, silence_error_code: str,
                                  silence_floor_override: int | None = None,
                                  tail_tolerance_samples: int | None = None) -> tuple[int, array]:
    threshold = timing_obj["silence_peak_pcm"]
    bridge = timing_obj["bridge_samples"]
    cues = timing_obj["cues"]
    silences = timing_obj["silences"]
    if type(threshold) is not int or not (0 <= threshold < 32768) or type(bridge) is not int or bridge < 0:
        raise SlotRefusal(2, "invalid_timing_policy")
    if type(cues) is not list or not cues or type(silences) is not list:
        raise SlotRefusal(2, "invalid_timing_cues")

    peaks, mono = _pcm_peaks_and_mono(pcm_bytes, channels)
    if len(peaks) != sample_count:
        raise SlotRefusal(4, "pcm_sample_count_mismatch")

    eff_silence_limit = threshold if silence_floor_override is None else max(threshold, silence_floor_override)
    first_frame, last_frame = timing_obj["start_frame"], timing_obj["end_frame"]
    samples_per_frame = Fraction(timing_obj["sample_rate"]) / Fraction(*timing_obj["fps"])
    if timing_obj.get("activity_by_cue") is True:
        observed = []
        cursor = 0
        for cue in cues:
            left, right = cue["start_frame"], cue["end_frame"]
            if not first_frame <= left < right <= last_frame:
                raise SlotRefusal(2, "invalid_cue_frame_bounds")
            start = ceil((left - first_frame) * samples_per_frame)
            stop = min(sample_count, ceil((right - first_frame) * samples_per_frame))
            if not cursor <= start < stop:
                raise SlotRefusal(2, "invalid_cue_frame_bounds")
            if any(peak > eff_silence_limit for peak in peaks[cursor:start]):
                raise SlotRefusal(5, silence_error_code)
            observed.extend((start + a, start + b) for a, b in
                            _activity_intervals(peaks[start:stop], eff_silence_limit, bridge))
            cursor = stop
        if any(peak > eff_silence_limit for peak in peaks[cursor:]):
            raise SlotRefusal(5, silence_error_code)
    else:
        observed = _activity_intervals(peaks, eff_silence_limit, bridge)
    if len(observed) != len(cues):
        raise SlotRefusal(5, onset_error_code)

    max_err = 0
    prev_end = 0
    # Older timing manifests describe an envelope tail using their existing gap allowance.
    tail_tolerance = max(tolerance_samples, bridge) if tail_tolerance_samples is None else tail_tolerance_samples
    for obs, cue in zip(observed, cues):
        exp_start = cue["expected_onset_sample"]
        exp_end = cue["expected_end_sample"]
        if not (prev_end <= exp_start < exp_end <= sample_count):
            raise SlotRefusal(2, "invalid_cue_sample_bounds")
        cue_first, cue_last = cue["start_frame"], cue["end_frame"]
        if not (first_frame <= cue_first < cue_last <= last_frame
                and ceil((cue_first - first_frame) * samples_per_frame) <= exp_start
                and exp_end <= ceil((cue_last - first_frame) * samples_per_frame)):
            raise SlotRefusal(2, "invalid_cue_frame_bounds")
        prev_end = exp_end
        err = abs(obs[0] - exp_start)
        max_err = max(max_err, err)
        if err > tolerance_samples:
            raise SlotRefusal(5, onset_error_code)

        if abs(obs[1] - exp_end) > tail_tolerance:
            raise SlotRefusal(5, onset_error_code.replace("onset", "tail"))

    for window in silences:
        if (type(window) is not list or len(window) != 2
                or any(type(x) is not int for x in window)
                or not (0 <= window[0] < window[1] <= sample_count)):
            raise SlotRefusal(2, "invalid_silence_window")
        if max(peaks[window[0]:window[1]]) > eff_silence_limit:
            raise SlotRefusal(5, silence_error_code)

    return max_err, mono


def _encode_delivery_mp4(master_mov: Path, delivery_mp4: Path, *, picture: dict, delivery_cfg: dict,
                         pcm: dict | None, ffmpeg: Path, scratch: Path, timeout_s: int) -> None:
    fps_num, fps_den = picture["fps"]
    crf = delivery_cfg["crf"]
    if not (0 <= crf <= 51):
        raise SlotRefusal(2, "invalid_delivery_crf")
    cmd = [
        ffmpeg, *_FFMPEG_BASE, *_LOCAL_PROTO,
        "-i", master_mov,
        "-map", "0:v:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", crf,
        "-pix_fmt", "yuv420p",
        "-fps_mode", "cfr", "-r", f"{fps_num}/{fps_den}",
        "-video_track_timescale", fps_num,
    ]
    if pcm is not None:
        bitrate = delivery_cfg["audio_bitrate_kbps"]
        if not (64 <= bitrate <= 512):
            raise SlotRefusal(2, "invalid_delivery_audio_bitrate")
        cmd += [
            "-map", "0:a:0",
            "-c:a", "aac", "-b:a", f"{bitrate}k",
            "-ar", pcm["sample_rate"],
            "-ac", pcm["channels"],
        ]
    else:
        cmd += ["-an"]
    movie_scale = lcm(fps_num, pcm["sample_rate"] if pcm else 1)
    cmd += [
        "-movie_timescale", movie_scale, "-map_metadata", "-1", "-map_chapters", "-1",
        "-movflags", "+faststart",
        "-f", "mp4", "-n", delivery_mp4,
    ]
    run_local_cmd(cmd, scratch / "delivery_encode.log", timeout_s, failure_code="delivery_encode_failed")


def _verify_delivery_mp4(delivery_mp4: Path, *, schedule: list[dict], picture: dict, delivery_cfg: dict,
                         pcm: dict | None, timing_obj: dict | None, source_mono: array | None,
                         ffmpeg: Path, ffprobe: Path, scratch: Path, timeout_s: int) -> dict:
    # 1. Full error-intolerant decode check mandated by RP3_PLAN.md Release Check.
    run_local_cmd(
        [
            ffmpeg, *_FFMPEG_BASE, *_LOCAL_PROTO,
            "-i", delivery_mp4, "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-",
        ],
        scratch / "delivery_null_decode.log",
        timeout_s,
        failure_code="delivery_null_decode_failed",
    )

    # 2. Stream and CFR video frame verification.
    probe_frames_path = scratch / "delivery_frames.json"
    run_local_cmd(
        [
            ffprobe, "-v", "error", "-threads", 1, *_LOCAL_PROTO, "-i", delivery_mp4,
            "-show_streams", "-show_frames",
            "-show_entries",
            "stream=index,codec_type,codec_name,pix_fmt,width,height,time_base,start_pts,duration_ts,sample_rate,channels:"
            "frame=stream_index,media_type,pts,duration,pkt_duration,width,height,nb_samples,channels",
            "-of", "json",
        ],
        probe_frames_path,
        timeout_s,
        failure_code="delivery_probe_frames_failed",
    )
    f_report, _ = load_strict_json(probe_frames_path, max_bytes=32 * 2**20)
    streams = f_report.get("streams", [])
    v_streams = [s for s in streams if s.get("codec_type") == "video"]
    a_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if len(v_streams) != 1 or len(a_streams) != bool(pcm) or len(streams) != 1 + bool(pcm):
        raise SlotRefusal(4, "delivery_stream_membership_mismatch")

    vs = v_streams[0]
    width, height = picture["width"], picture["height"]
    if (vs.get("codec_name"), vs.get("pix_fmt"), vs.get("width"), vs.get("height")) != ("h264", "yuv420p", width, height):
        raise SlotRefusal(4, "delivery_video_format_mismatch")
    v_base = Fraction(vs["time_base"])
    if v_base <= 0 or int(vs["start_pts"]) != 0 or int(vs["duration_ts"]) <= 0:
        raise SlotRefusal(4, "delivery_video_clock_mismatch")
    if v_base * int(vs["duration_ts"]) != Fraction(*schedule[-1]["time"][1]):
        raise SlotRefusal(4, "delivery_video_duration_mismatch")

    v_frames = [r for r in f_report.get("frames", []) if r.get("stream_index") == vs["index"]]
    if len(v_frames) != len(schedule):
        raise SlotRefusal(4, "delivery_video_frame_count_mismatch")
    for row, slot in zip(v_frames, schedule):
        start_q, end_q = (Fraction(*p) for p in slot["time"])
        pts = int(row["pts"])
        ticks = _frame_duration_ticks(row)
        if v_base * pts != start_q or v_base * ticks != end_q - start_q:
            raise SlotRefusal(4, "delivery_video_cfr_pts_mismatch")

    if pcm is None:
        return {
            "delivery_sha256": sha256_file_hex(delivery_mp4),
            "video_codec": "h264",
            "pix_fmt": "yuv420p",
            "video_frames_verified": len(v_frames),
            "audio_present": False,
            "leading_priming_samples": None,
            "trailing_padding_samples": None,
            "max_delivery_onset_error_samples": None,
            "delivery_pcm_correlation_ppm": None,
        }

    # 3. AAC packet clock and MP4 edit-list priming witness verification.
    as_info = a_streams[0]
    rate = pcm["sample_rate"]
    channels = pcm["channels"]
    sample_count = pcm["sample_count"]
    if (as_info.get("codec_name"), int(as_info.get("sample_rate", 0)), as_info.get("channels")) != ("aac", rate, channels):
        raise SlotRefusal(4, "delivery_audio_format_mismatch")
    a_base = Fraction(as_info["time_base"])
    if a_base <= 0 or int(as_info.get("start_pts", -1)) != 0:
        raise SlotRefusal(4, "delivery_priming_compensation_missing")
    if a_base * int(as_info.get("duration_ts", 0)) * rate != sample_count:
        raise SlotRefusal(4, "delivery_audio_duration_mismatch")

    pkts_elst_path = scratch / "delivery_audio_pkts_elst.json"
    pkts_raw_path = scratch / "delivery_audio_pkts_raw.json"
    run_local_cmd(
        [
            ffprobe, "-v", "error", "-threads", 1, *_LOCAL_PROTO,
            "-select_streams", "a:0", "-show_packets", "-of", "json", delivery_mp4,
        ],
        pkts_elst_path,
        timeout_s,
        failure_code="delivery_probe_packets_failed",
    )
    run_local_cmd(
        [
            ffprobe, "-v", "error", "-threads", 1, *_LOCAL_PROTO, "-ignore_editlist", "1",
            "-select_streams", "a:0", "-show_packets", "-of", "json", delivery_mp4,
        ],
        pkts_raw_path,
        timeout_s,
        failure_code="delivery_probe_raw_packets_failed",
    )
    pkts_elst = load_strict_json(pkts_elst_path, max_bytes=16 * 2**20)[0].get("packets", [])
    pkts_raw = load_strict_json(pkts_raw_path, max_bytes=16 * 2**20)[0].get("packets", [])
    if not pkts_elst or len(pkts_elst) != len(pkts_raw):
        raise SlotRefusal(4, "delivery_audio_packets_invalid")

    pkt0 = pkts_elst[0]
    pkt0_pts = int(pkt0.get("pts", 0))
    skip_entries = [
        sd for sd in pkt0.get("side_data_list", [])
        if sd.get("side_data_type") == "Skip Samples"
    ]
    if pkt0_pts >= 0 or not skip_entries:
        raise SlotRefusal(4, "delivery_priming_compensation_missing")
    leading_priming = int(skip_entries[0].get("skip_samples", 0))
    if leading_priming <= 0 or pkt0_pts != -leading_priming:
        raise SlotRefusal(4, "delivery_priming_compensation_missing")

    aac_frame_samples = 1024
    untrimmed_codec_samples = len(pkts_elst) * aac_frame_samples
    trailing_padding = untrimmed_codec_samples - leading_priming - sample_count
    last_end_samples = int(a_base * (int(pkts_elst[-1]["pts"]) + int(pkts_elst[-1]["duration"])) * rate)
    if not (0 <= trailing_padding < aac_frame_samples * 2) or last_end_samples != sample_count:
        raise SlotRefusal(4, "delivery_audio_packet_trim_mismatch")

    # 4. Decode delivery audio with container priming compensation applied and verify sync & waveform.
    dec_pcm_path = scratch / "delivery_decoded.pcm"
    run_local_cmd(
        [
            ffmpeg, *_FFMPEG_BASE, *_LOCAL_PROTO, "-i", delivery_mp4,
            "-map", "0:a:0", "-c:a", "pcm_s16le", "-f", "s16le", "-n", dec_pcm_path,
        ],
        scratch / "delivery_decode_audio.log",
        timeout_s,
        failure_code="delivery_decode_audio_failed",
    )
    bytes_per_frame = 2 * channels
    if dec_pcm_path.stat().st_size > (sample_count + trailing_padding) * bytes_per_frame:
        raise SlotRefusal(4, "delivery_decoded_sample_count_mismatch")
    dec_bytes_all = dec_pcm_path.read_bytes()
    if len(dec_bytes_all) % bytes_per_frame != 0:
        raise SlotRefusal(4, "delivery_decoded_pcm_alignment_invalid")
    dec_frames_total = len(dec_bytes_all) // bytes_per_frame
    if not (sample_count <= dec_frames_total <= sample_count + trailing_padding):
        raise SlotRefusal(4, "delivery_decoded_sample_count_mismatch")
    dec_bytes = dec_bytes_all[:sample_count * bytes_per_frame]

    assert timing_obj is not None and source_mono is not None
    delivery_tol = timing_obj["delivery_onset_tolerance_samples"]
    max_deliv_err, deliv_mono = _verify_pcm_cues_and_silences(
        dec_bytes,
        channels,
        sample_count,
        timing_obj,
        tolerance_samples=delivery_tol,
        onset_error_code="delivery_audio_onset_shifted",
        silence_error_code="delivery_labelled_silence_contains_audio",
        silence_floor_override=350,
        tail_tolerance_samples=timing_obj.get("delivery_tail_tolerance_samples"),
    )
    corr_ppm = _pearson_correlation_ppm(source_mono, deliv_mono)
    if corr_ppm < delivery_cfg["min_pcm_correlation_ppm"]:
        raise SlotRefusal(5, "delivery_audio_waveform_correlation_below_floor")

    return {
        "delivery_sha256": sha256_file_hex(delivery_mp4),
        "video_codec": "h264",
        "pix_fmt": "yuv420p",
        "video_frames_verified": len(v_frames),
        "audio_present": True,
        "leading_priming_samples": leading_priming,
        "trailing_padding_samples": trailing_padding,
        "max_delivery_onset_error_samples": max_deliv_err,
        "delivery_pcm_correlation_ppm": corr_ppm,
    }


def _empty_metrics(wall_ms: int | None = None) -> dict:
    return {
        "wall_ms": wall_ms,
        "frame_count": None,
        "sample_rate": None,
        "sample_count": None,
        "channels": None,
        "leading_priming_samples": None,
        "trailing_padding_samples": None,
        "max_lossless_onset_error_samples": None,
        "max_delivery_onset_error_samples": None,
        "delivery_pcm_correlation_ppm": None,
    }


def _in_audio_scope() -> bool:
    """Verify the live scope that the native process runner reads for its limits."""
    try:
        root = Path("/sys/fs/cgroup")
        groups = Path("/proc/self/cgroup").read_text().splitlines()
        relative = next(line.removeprefix("0::/") for line in groups if line.startswith("0::/"))
        scope = (root / relative).resolve()
        return (scope.is_relative_to(root) and scope.suffix == ".scope"
                and all((scope / filename).read_text().strip() == str(limit)
                        for _, filename, limit in _SCOPE_LIMITS)
                and os.nice(0) >= 15 and os.sched_getaffinity(0) <= set(range(16)))
    except (OSError, RuntimeError, StopIteration):
        return False


def _run_in_audio_scope(command: str, request_path: Path, output_dir: Path, *,
                        factory_root: Path | None, require_factory_mux: bool,
                        overwrite: bool = False) -> dict:
    # The marker prevents recursive launches. Only the kernel values prove limits.
    if os.environ.get(_SCOPE_CHILD) == "1":
        raise SlotRefusal(3, "memory_limits_unproven")

    argv = ["systemd-run", "--user", "--scope", "--quiet", "--expand-environment=no"]
    for prop, _, limit in _SCOPE_LIMITS:
        argv.extend(("-p", f"{prop}={limit}"))
    argv.extend(("--", "nice", "-n", "15", "taskset", "-c", "0-15", sys.executable, "-B",
                 str(Path(__file__).resolve()), command, "--request", str(request_path),
                 "--output", str(output_dir)))
    if factory_root is not None:
        argv.extend(("--factory-root", str(Path(factory_root).resolve())))
    if require_factory_mux:
        argv.append("--require-factory-mux")
    if overwrite:
        argv.append("--overwrite")
    try:
        completed = subprocess.run(argv, env={**os.environ, **_LOCALE, _SCOPE_CHILD: "1"},
                                   capture_output=True, check=False)
    except OSError as exc:
        raise SlotRefusal(3, "memory_limits_unproven") from exc
    entered = _SCOPE_ENTERED.encode("ascii") in completed.stderr.splitlines()
    if completed.returncode < 0 or completed.returncode == 137:
        raise SlotRefusal(6, "scope_oom_or_signal_killed")
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        if not entered:
            raise SlotRefusal(3, "memory_limits_unproven") from exc
        if b"Traceback (most recent call last):" in completed.stderr:
            raise SlotRefusal(2, "child_unhandled_exception") from exc
        raise SlotRefusal(2, f"child_result_missing:{completed.returncode}") from exc
    if (not isinstance(result, dict) or type(result.get("rc")) is not int
            or result["rc"] not in RC_VALUES or result["rc"] != completed.returncode):
        raise SlotRefusal(2, "child_result_invalid")
    return result


def _configured_factory(request: dict, base: Path, explicit_root: Path | None) -> Path | None:
    tc = request.get("toolchain")
    if explicit_root is None and isinstance(tc, dict) and tc.get("factory_root"):
        return resolve_local_path(base, tc["factory_root"], role="factory_root")
    return explicit_root


def _toolchain(request: dict) -> tuple[Path, Path, int]:
    config = request["toolchain"]
    ffmpeg = Path(config["ffmpeg"]).resolve()
    ffprobe = Path(config["ffprobe"]).resolve()
    timeout_s = config["timeout_s"]
    if not (1 <= timeout_s <= 86400) or not ffmpeg.is_file() or not ffprobe.is_file():
        raise SlotRefusal(3, "toolchain_unavailable")
    return ffmpeg, ffprobe, timeout_s


def run_slot(request_path: Path, output_dir: Path, *, factory_root: Path | None = None,
             require_factory_mux: bool = False, overwrite: bool = False) -> dict:
    t0 = time.monotonic_ns()
    request_path = Path(request_path).resolve()
    output_dir = Path(output_dir).resolve()
    contract_path = Path(__file__).resolve().parent / "contract.json"

    req_id = "unknown"
    mode = "silent"
    route = "unknown"
    domain = "unknown"
    mux_provider = "unresolved"
    owns_output = False

    try:
        req_obj, req_raw = load_strict_json(request_path)
        if type(req_obj) is not dict:
            raise SlotRefusal(2, "invalid_request_object")
        req_id = str(req_obj.get("request_id", "unknown"))
        if req_obj.get("mode") in ("narrated", "silent"):
            mode = req_obj["mode"]
        if type(req_obj.get("route")) is str:
            route = req_obj["route"]

        domain = _voice_domain(req_obj)
        if not _in_audio_scope():
            return _run_in_audio_scope("run", request_path, output_dir, factory_root=factory_root,
                                       require_factory_mux=require_factory_mux, overwrite=overwrite)

        cfg_factory_root = _configured_factory(req_obj, request_path.parent, factory_root)
        af_core, pmux, mux_provider, mux_module_sha = load_mux_backend(
            cfg_factory_root, require_factory_mux=require_factory_mux
        )
        validate_against_contract(req_obj, contract_path, af_core=af_core)

        if output_dir.exists() and any(output_dir.iterdir()):
            if not overwrite:
                raise SlotRefusal(5, "output_exists")
        output_dir.mkdir(parents=True, exist_ok=True)
        owns_output = True

        ffmpeg, ffprobe, timeout_s = _toolchain(req_obj)

        wav_path, pcm, timing_obj, silence_disp, consent_obj = _validate_voice_route_and_inputs(
            req_obj, request_path.parent, pmux
        )

        schedule = _build_schedule(req_obj["picture"], pcm)
        fps_q, movie_scale = _call_pmux(pmux.validate_timing, schedule, req_obj["picture"]["fps"], pcm)

        max_lossless_err = None
        source_mono = None
        if pcm is not None and wav_path is not None and timing_obj is not None:
            with wave.open(str(wav_path), "rb") as wf:
                raw_pcm_bytes = wf.readframes(pcm["sample_count"])
            max_lossless_err, source_mono = _verify_pcm_cues_and_silences(
                raw_pcm_bytes,
                pcm["channels"],
                pcm["sample_count"],
                timing_obj,
                tolerance_samples=timing_obj["onset_tolerance_samples"],
                onset_error_code="audio_onset_shifted",
                silence_error_code="labelled_silence_contains_audio",
                tail_tolerance_samples=timing_obj.get("tail_tolerance_samples"),
            )

        with tempfile.TemporaryDirectory(prefix=".audio-scratch-", dir=output_dir) as work_tmp:
            scratch = Path(work_tmp)
            staged_frames, frame_digests = _stage_picture_frames(
                req_obj["picture"], request_path.parent, scratch, ffmpeg, timeout_s
            )
            master_stage = scratch / "master.mov"
            # Both admission backends use the same deterministic media child runner.
            _PortablePresentationMux.mux(
                staged_frames, master_stage, schedule=schedule, fps_q=fps_q, movie_scale=movie_scale,
                audio=wav_path, pcm=pcm, ffmpeg=ffmpeg, timeout=timeout_s, scratch=scratch,
            )
            master_dec = _verify_master(
                master_stage, scratch, schedule=schedule, width=req_obj["picture"]["width"],
                height=req_obj["picture"]["height"], frame_digests=frame_digests, pcm=pcm,
                ffmpeg=ffmpeg, ffprobe=ffprobe, timeout=timeout_s,
            )

            delivery_stage = None
            delivery_report = None
            if req_obj["delivery"]["enabled"]:
                delivery_stage = scratch / "delivery.mp4"
                _encode_delivery_mp4(
                    master_stage,
                    delivery_stage,
                    picture=req_obj["picture"],
                    delivery_cfg=req_obj["delivery"],
                    pcm=pcm,
                    ffmpeg=ffmpeg,
                    scratch=scratch,
                    timeout_s=timeout_s,
                )
                delivery_report = _verify_delivery_mp4(
                    delivery_stage,
                    schedule=schedule,
                    picture=req_obj["picture"],
                    delivery_cfg=req_obj["delivery"],
                    pcm=pcm,
                    timing_obj=timing_obj,
                    source_mono=source_mono,
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    scratch=scratch,
                    timeout_s=timeout_s,
                )

            master_out = output_dir / "master.mov"
            os.replace(master_stage, master_out)
            master_sha = sha256_file_hex(master_out)
            if master_sha != master_dec["film_sha256"]:
                raise SlotRefusal(4, "master_changed_on_publish")

            outputs = [
                {
                    "role": "lossless_master_mov",
                    "path": "master.mov",
                    "media_type": "video/quicktime",
                    "sha256": "sha256:" + master_sha,
                }
            ]
            if delivery_stage is not None and delivery_report is not None:
                delivery_out = output_dir / "delivery.mp4"
                os.replace(delivery_stage, delivery_out)
                deliv_sha = sha256_file_hex(delivery_out)
                if deliv_sha != delivery_report["delivery_sha256"]:
                    raise SlotRefusal(4, "delivery_changed_on_publish")
                outputs.append({
                    "role": "delivery_mp4",
                    "path": "delivery.mp4",
                    "media_type": "video/mp4",
                    "sha256": "sha256:" + deliv_sha,
                })

            wall_ms = (time.monotonic_ns() - t0) // 1_000_000
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "rc": 0,
                "status": "OK",
                "verdict": "PASS",
                "request_id": req_id,
                "request_sha256": "sha256:" + hashlib.sha256(req_raw).hexdigest(),
                "voice_domain": domain,
                "product_narrator_separation": (
                    "enforced_distinct_from_tasklight_swarm_narrator" if domain == LEGACY_VOICE_DOMAIN
                    else "voice_domain_bound"
                ),
                "master_verification": "streaming_rgb24_sha256_and_pcm_s16le_sha256",
                "mode": mode,
                "route": route,
                "mux_provider": mux_provider,
                "mux_module_sha256": "sha256:" + mux_module_sha,
                "slot_sha256": "sha256:" + sha256_file_hex(Path(__file__).resolve()),
                "master": {
                    "film_sha256": "sha256:" + master_sha,
                    "video_time_base": master_dec["video_time_base"],
                    "video_frames": len(master_dec["video"]),
                    "audio_time_base": master_dec["audio_time_base"],
                    "audio_packets": len(master_dec["audio"]),
                    "pcm": (
                        {
                            "sample_rate": pcm["sample_rate"],
                            "sample_count": pcm["sample_count"],
                            "channels": pcm["channels"],
                            "pcm_sha256": "sha256:" + pcm["pcm_sha256"],
                        }
                        if pcm is not None else None
                    ),
                },
                "delivery": delivery_report,
                "silence_disposition": silence_disp,
                "consent_verified": consent_obj is not None,
            }
            receipt_path = output_dir / "receipt.json"
            atomic_write_json(receipt_path, receipt)
            receipt_sha = "sha256:" + sha256_file_hex(receipt_path)
            outputs.append({
                "role": "audio_mux_receipt",
                "path": "receipt.json",
                "media_type": "application/json",
                "sha256": receipt_sha,
            })

            absence = {}
            if mode == "silent":
                absence["narration_audio"] = silence_disp["disposition"] if silence_disp else "signed_silence"

            result = {
                "schema": SCHEMA_ID,
                "kind": "result",
                "slot": "audio",
                "request_id": req_id,
                "rc": 0,
                "status": "OK",
                "verdict": "PASS",
                "voice_domain": domain,
                "mode": mode,
                "route": route,
                "mux_provider": mux_provider,
                "reason_codes": [],
                "outputs": outputs,
                "receipts": [
                    {
                        "schema": RECEIPT_SCHEMA,
                        "path": "receipt.json",
                        "sha256": receipt_sha,
                    }
                ],
                "metrics": {
                    "wall_ms": wall_ms,
                    "frame_count": len(schedule),
                    "sample_rate": pcm["sample_rate"] if pcm else None,
                    "sample_count": pcm["sample_count"] if pcm else None,
                    "channels": pcm["channels"] if pcm else None,
                    "leading_priming_samples": delivery_report["leading_priming_samples"] if delivery_report else None,
                    "trailing_padding_samples": delivery_report["trailing_padding_samples"] if delivery_report else None,
                    "max_lossless_onset_error_samples": max_lossless_err,
                    "max_delivery_onset_error_samples": delivery_report["max_delivery_onset_error_samples"] if delivery_report else None,
                    "delivery_pcm_correlation_ppm": delivery_report["delivery_pcm_correlation_ppm"] if delivery_report else None,
                },
                "absence_reasons": absence,
            }
            validate_against_contract(result, contract_path, af_core=af_core)
            atomic_write_json(output_dir / "result.json", result)
            return result

    except SlotRefusal as exc:
        wall_ms = (time.monotonic_ns() - t0) // 1_000_000
        result = {
            "schema": SCHEMA_ID,
            "kind": "result",
            "slot": "audio",
            "request_id": req_id,
            "rc": exc.rc,
            "status": STATUS_BY_RC[exc.rc],
            "verdict": VERDICT_BY_RC[exc.rc],
            "voice_domain": domain,
            "mode": mode,
            "route": route,
            "mux_provider": mux_provider,
            "reason_codes": [exc.reason],
            "outputs": [],
            "receipts": [],
            "metrics": _empty_metrics(wall_ms),
            "absence_reasons": {},
        }
        if owns_output:
            atomic_write_json(output_dir / "result.json", result)
        return result


def verify_run(request_path: Path, output_dir: Path, *, factory_root: Path | None = None,
               require_factory_mux: bool = False) -> dict:
    """Independently re-verify an existing AUDIO slot output directory against its request."""
    t0 = time.monotonic_ns()
    request_path = Path(request_path).resolve()
    output_dir = Path(output_dir).resolve()
    contract_path = Path(__file__).resolve().parent / "contract.json"
    try:
        if not _in_audio_scope():
            return _run_in_audio_scope("verify", request_path, output_dir, factory_root=factory_root,
                                       require_factory_mux=require_factory_mux)
        req_obj, _ = load_strict_json(request_path)
        if type(req_obj) is not dict:
            raise SlotRefusal(2, "invalid_request_object")
        _voice_domain(req_obj)
        cfg_factory_root = _configured_factory(req_obj, request_path.parent, factory_root)
        af_core, pmux, mux_provider, _ = load_mux_backend(cfg_factory_root, require_factory_mux=require_factory_mux)
        validate_against_contract(req_obj, contract_path, af_core=af_core)

        wav_path, pcm, timing_obj, _, _ = _validate_voice_route_and_inputs(req_obj, request_path.parent, pmux)
        schedule = _build_schedule(req_obj["picture"], pcm)
        _call_pmux(pmux.validate_timing, schedule, req_obj["picture"]["fps"], pcm)
        source_mono = None
        if pcm is not None and wav_path is not None and timing_obj is not None:
            with wave.open(str(wav_path), "rb") as wf:
                raw_pcm_bytes = wf.readframes(pcm["sample_count"])
            _, source_mono = _verify_pcm_cues_and_silences(
                raw_pcm_bytes,
                pcm["channels"],
                pcm["sample_count"],
                timing_obj,
                tolerance_samples=timing_obj["onset_tolerance_samples"],
                onset_error_code="audio_onset_shifted",
                silence_error_code="labelled_silence_contains_audio",
                tail_tolerance_samples=timing_obj.get("tail_tolerance_samples"),
            )

        ffmpeg, ffprobe, timeout_s = _toolchain(req_obj)

        master_mov = output_dir / "master.mov"
        if not master_mov.is_file():
            raise SlotRefusal(3, "master_mov_missing")

        with tempfile.TemporaryDirectory(prefix=".verify-scratch-") as work_tmp:
            scratch = Path(work_tmp)
            _, frame_digests = _stage_picture_frames(
                req_obj["picture"], request_path.parent, scratch, ffmpeg, timeout_s, stage=False
            )
            _verify_master(
                master_mov, scratch, schedule=schedule, width=req_obj["picture"]["width"],
                height=req_obj["picture"]["height"], frame_digests=frame_digests, pcm=pcm,
                ffmpeg=ffmpeg, ffprobe=ffprobe, timeout=timeout_s,
            )

            delivery_report = None
            if req_obj["delivery"]["enabled"]:
                delivery_mp4 = output_dir / "delivery.mp4"
                if not delivery_mp4.is_file():
                    raise SlotRefusal(3, "delivery_mp4_missing")
                delivery_report = _verify_delivery_mp4(
                    delivery_mp4,
                    schedule=schedule,
                    picture=req_obj["picture"],
                    delivery_cfg=req_obj["delivery"],
                    pcm=pcm,
                    timing_obj=timing_obj,
                    source_mono=source_mono,
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    scratch=scratch,
                    timeout_s=timeout_s,
                )

        wall_ms = (time.monotonic_ns() - t0) // 1_000_000
        return {
            "rc": 0,
            "status": "OK",
            "verdict": "PASS",
            "mux_provider": mux_provider,
            "reason_codes": [],
            "wall_ms": wall_ms,
            "delivery": delivery_report,
        }
    except SlotRefusal as exc:
        wall_ms = (time.monotonic_ns() - t0) // 1_000_000
        return {
            "rc": exc.rc,
            "status": STATUS_BY_RC[exc.rc],
            "verdict": VERDICT_BY_RC[exc.rc],
            "reason_codes": [exc.reason],
            "wall_ms": wall_ms,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("run", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--request", required=True, type=Path)
        p.add_argument("--output", required=True, type=Path)
        p.add_argument("--factory-root", type=Path, default=None)
        p.add_argument("--require-factory-mux", action="store_true")
        if name == "run":
            p.add_argument("--overwrite", action="store_true")

    args = parser.parse_args(argv)
    if os.environ.get(_SCOPE_CHILD) == "1" and _in_audio_scope():
        print(_SCOPE_ENTERED, file=sys.stderr, flush=True)
    try:
        if args.cmd == "run":
            out = run_slot(
                args.request,
                args.output,
                factory_root=args.factory_root,
                require_factory_mux=args.require_factory_mux,
                overwrite=args.overwrite,
            )
        else:
            out = verify_run(
                args.request,
                args.output,
                factory_root=args.factory_root,
                require_factory_mux=args.require_factory_mux,
            )
    except (OSError, MemoryError) as exc:
        rc = 6 if isinstance(exc, MemoryError) else 2
        reason = "child_memory_exhausted" if rc == 6 else f"filesystem_error:{exc.errno}"
        out = {"rc": rc, "status": STATUS_BY_RC[rc], "verdict": VERDICT_BY_RC[rc], "reason_codes": [reason]}
    sys.stdout.write(canonical_json_bytes(out).decode("utf-8"))
    return 0 if out["rc"] == 0 else out["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
