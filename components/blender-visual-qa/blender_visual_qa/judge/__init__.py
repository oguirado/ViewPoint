"""Multimodal judge module for visual QA (unit B3).

Implements the S3 design decisions:
- D-S3-1: Finite Python judge, no persistent service/MCP.
- D-S3-2: Direct dispatcher argv-array call (gemini-3.8-flash, pleno, medium).
- D-S3-3: Media/rubric only; withhold telemetry, oracle verdicts, mutant labels, canary expectations.
- D-S3-4: PNG for HUD, pair/sheet for cards, MP4 for motion/FX with source maps.
- D-S3-5: Companion attention PNG+MP4 every attempt, expected answers held locally.
- D-S3-6: Accept PASS/FAIL confidence >=0.85 after validation; lower -> normalized CANNOT_SEE.
- D-S3-7: One retry per abstaining batch within caps; alternative evidence; accepted FAIL never erased.
- D-S3-8: Dual oracle reconciliation compliance.
- D-S3-9: Four primary calls, two retries max (6 calls cap); 180k tokens total, 35k/call, 180s call, 900s wall.
- D-S3-10: Blinded calibration and strict validation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import tempfile
import time
from typing import Any
import zlib

from blender_visual_qa import contracts

# Schema and identity constants
SCHEMA_JUDGE_STAGE = "vqa.judge-stage/v1"
SCHEMA_JUDGE_INPUT = "vqa.judge-input/v1"
SCHEMA_JUDGE_OUTPUT = "vqa.judge-output/v1"
SCHEMA_JUDGE_EXPECTATIONS = "vqa.judge-expectations/v1"
SCHEMA_JUDGE_POLICY = "vqa.judge-policy/v1"
DEFAULT_MODEL_ID = "gemini-3.8-flash"
DEFAULT_EFFORT = "medium"
DEFAULT_PERMISSION = "pleno"
DEFAULT_VIA = "directo"
CONFIDENCE_MIN = 0.85

# Budget and call caps (R-9 / L-6)
MAX_CALLS = 6
MAX_RETRIES_PER_BATCH = 1
MAX_RETRY_CALLS = 2
CALL_TIMEOUT_S = 480
WALL_BUDGET_S = 2400
INPUT_BUDGET_TOKENS = 1500000
INPUT_PER_CALL_TOKENS = 300000
ADMISSION_INPUT_LIMIT = 1500000
MAX_GAMEPLAY_MEDIA_PER_BATCH = 3
DEFAULT_TRANSPORT_RETRY_BACKOFF_S = 20.0

# Canary alphabet and font (L-5)
CANARY_ALPHABET = "ACDEFHJKLMNPRTUVWXY3479"
DEFAULT_CANARY_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

# Canonical worker command and transport defaults (L-3); override via environment variables.
DEFAULT_WORKER_PATH = "scr-obrero-gemini"
DEFAULT_AGY_SHIM = "agy"
DEFAULT_WORKER_ARGV_PREFIX = [os.environ.get("BLENDER_VISUAL_QA_GEMINI_WORKER", DEFAULT_WORKER_PATH)]

DEFAULT_POLICY = {
    "schema": SCHEMA_JUDGE_POLICY,
    "transport": "newton-gemini-worker",
    "model_id": DEFAULT_MODEL_ID,
    "effort": DEFAULT_EFFORT,
    "permission": DEFAULT_PERMISSION,
    "via": DEFAULT_VIA,
    "confidence_min": CONFIDENCE_MIN,
    "max_calls": MAX_CALLS,
    "max_retries_per_batch": MAX_RETRIES_PER_BATCH,
    "max_retry_calls": MAX_RETRY_CALLS,
    "call_timeout_s": CALL_TIMEOUT_S,
    "wall_budget_s": WALL_BUDGET_S,
    "input_budget_tokens": INPUT_BUDGET_TOKENS,
    "input_per_call_tokens": INPUT_PER_CALL_TOKENS,
}


def get_default_judge_policy() -> dict[str, Any]:
    """Get canonical default judge policy dictionary per R-9."""
    return dict(DEFAULT_POLICY)


def get_canary_font_path(custom_path: str | Path | None = None) -> Path:
    """Resolve and validate canary font path from arg, env, or default."""
    font_str = custom_path or os.environ.get("BLENDER_VISUAL_QA_CANARY_FONT", DEFAULT_CANARY_FONT)
    font_path = Path(font_str).resolve()
    if not font_path.is_file():
        raise JudgeValidationError(
            f"Canary TrueType font not found at {font_str}",
            code="missing_canary_font",
        )
    return font_path


def derive_canary_token(seed: str, length: int = 8) -> str:
    """Derive deterministic canary token using only CANARY_ALPHABET."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    num = int.from_bytes(digest, "big")
    alphabet_len = len(CANARY_ALPHABET)
    chars = []
    for _ in range(length):
        chars.append(CANARY_ALPHABET[num % alphabet_len])
        num //= alphabet_len
    return "".join(chars)



def get_default_worker_argv_prefix() -> list[str]:
    """Get default worker argv prefix, respecting BLENDER_VISUAL_QA_GEMINI_WORKER env."""
    worker = os.environ.get("BLENDER_VISUAL_QA_GEMINI_WORKER", DEFAULT_WORKER_PATH)
    return [worker]


def get_default_agy_shim() -> str:
    """Get default agy shim path, respecting BLENDER_VISUAL_QA_AGY env."""
    return os.environ.get("BLENDER_VISUAL_QA_AGY", DEFAULT_AGY_SHIM)


class JudgeError(Exception):
    """Base error for judge operations."""

    def __init__(self, message: str, code: str = "judge_error", stage: str = "judge") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.stage = stage


class JudgeValidationError(JudgeError):
    """Validation or schema failure in judge."""

    def __init__(self, message: str, code: str = "validation_error") -> None:
        super().__init__(message, code=code)


class JudgeTransportError(JudgeError):
    """Worker transport or execution failure."""

    def __init__(self, message: str, code: str = "transport_error") -> None:
        super().__init__(message, code=code)


class JudgeBudgetError(JudgeError):
    """Budget or limit exhaustion in judge."""

    def __init__(self, message: str, code: str = "budget_exceeded") -> None:
        super().__init__(message, code=code)


def compute_canonical_sha256(data: Any) -> str:
    """Compute sha256 hex digest of canonically serialized JSON."""
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_file_sha256(path: Path) -> str:
    """Compute sha256 hex digest of file contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def strict_json_loads(text: str) -> dict[str, Any]:
    """Strictly parse JSON with duplicate key, NaN, Infinity and trailing byte rejection."""
    s = text.strip()
    if s.startswith("```") or s.endswith("```"):
        raise JudgeValidationError("Markdown fences forbidden in model output", code="output_markdown_fence")
    if not (s.startswith("{") and s.endswith("}")):
        raise JudgeValidationError("Model output must be exactly one JSON object without surrounding prose", code="output_not_single_json_object")

    def _reject_constant(c: str) -> None:
        raise JudgeValidationError(f"Forbidden constant {c!r} in JSON", code="output_forbidden_constant")

    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for k, v in pairs:
            if k in obj:
                raise JudgeValidationError(f"Duplicate key {k!r} in JSON", code="output_duplicate_key")
            obj[k] = v
        return obj

    dec = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    try:
        obj, idx = dec.raw_decode(s)
    except Exception as exc:
        raise JudgeValidationError(f"Strict JSON decoding failed: {exc}", code="output_malformed_json") from exc

    if s[idx:].strip():
        raise JudgeValidationError("Trailing bytes after JSON object", code="output_trailing_bytes")

    if not isinstance(obj, dict):
        raise JudgeValidationError("Expected top-level JSON object", code="output_not_object")
    return obj


# ---------------------------------------------------------------------------
# Canary Atlas & Media Encoding
# ---------------------------------------------------------------------------

# @anchor:bv.judge.png
def encode_png_rgb(width: int, height: int, pixels_rgb: bytes) -> bytes:
    """Encode raw RGB24 bytes to standard PNG format without external libraries."""
    raw_scanlines = bytearray()
    row_bytes = width * 3
    for y in range(height):
        raw_scanlines.append(0)  # filter type 0 (None)
        raw_scanlines.extend(pixels_rgb[y * row_bytes : (y + 1) * row_bytes])

    compressed = zlib.compress(bytes(raw_scanlines), level=6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


def render_canary_background(width: int = 320, height: int = 96, draw_shapes: bool = True) -> bytes:
    """Render 320x96 black background with optional cyan square and yellow circle shapes."""
    buf = bytearray(width * height * 3)  # default black background
    if draw_shapes:
        # Cyan square on left: solid RGB(0, 255, 255)
        # 30x30 square at (20, 33)
        sq_x0, sq_x1 = 20, 50
        sq_y0, sq_y1 = 33, 63
        for y in range(sq_y0, sq_y1):
            for x in range(sq_x0, sq_x1):
                idx = (y * width + x) * 3
                buf[idx : idx + 3] = b"\x00\xff\xff"

        # Yellow circle on right: solid RGB(255, 255, 0)
        # Center (280, 48), radius 16
        center_x, center_y, radius = 280, 48, 16
        r_sq = radius * radius
        for y in range(center_y - radius, center_y + radius + 1):
            for x in range(center_x - radius, center_x + radius + 1):
                if (x - center_x) ** 2 + (y - center_y) ** 2 <= r_sq:
                    if 0 <= x < width and 0 <= y < height:
                        idx = (y * width + x) * 3
                        buf[idx : idx + 3] = b"\xff\xff\x00"

    return bytes(buf)


def render_canary_frame(
    text: str,
    font_path: Path | str | None = None,
    width: int = 320,
    height: int = 96,
    draw_shapes: bool = True,
    fontsize: int = 32,
) -> bytes:
    """Render a 320x96 raw RGB24 frame with ffmpeg drawtext filter using TrueType font."""
    resolved_font = get_canary_font_path(font_path)
    ffmpeg_bin = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    if not os.path.exists(ffmpeg_bin):
        raise JudgeTransportError("ffmpeg executable not found for canary rendering")

    bg_bytes = render_canary_background(width, height, draw_shapes=draw_shapes)
    escaped_text = text.replace("\\", "\\\\").replace("'", "'\\''").replace(":", "\\:")
    cmd = [
        ffmpeg_bin,
        "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{width}x{height}",
        "-pix_fmt", "rgb24",
        "-i", "-",
        "-vf", f"drawtext=fontfile='{resolved_font}':text='{escaped_text}':fontsize={fontsize}:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2",
        "-frames:v", "1",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=bg_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=15,
        )
        return proc.stdout
    except subprocess.SubprocessError as exc:
        raise JudgeTransportError(f"Failed to render canary frame: {exc}") from exc


def create_attention_png(
    run_id: str,
    batch_id: str,
    attempt_id: str,
    out_path: Path,
    font_path: Path | str | None = None,
) -> dict[str, Any]:
    """Create 320x96 attention PNG fixture with derived unambiguous token and shapes."""
    rev = "v1"
    token_seed = f"{run_id}:{batch_id}:{attempt_id}:{rev}"
    token_text = derive_canary_token(token_seed, length=8)

    w, h = 320, 96
    resolved_font = get_canary_font_path(font_path)
    rgb_bytes = render_canary_frame(token_text, font_path=resolved_font, width=w, height=h, draw_shapes=True, fontsize=32)
    png_data = encode_png_rgb(w, h, rgb_bytes)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(png_data)

    sha256 = hashlib.sha256(png_data).hexdigest()
    return {
        "token": token_text,
        "path": out_path,
        "sha256": sha256,
        "kind": "png",
        "width": w,
        "height": h,
    }


def create_attention_video(
    run_id: str,
    batch_id: str,
    attempt_id: str,
    out_path: Path,
    font_path: Path | str | None = None,
) -> dict[str, Any]:
    """Create 4-second 1fps attention MP4 video fixture with temporal unambiguous tokens (L-7).

    Token A is visible for >= 2 s (seconds 0-1), then token B for >= 2 s (seconds 2-3).
    Total duration: 4.0 s, 4 frames at 1 fps.
    """
    rev = "v1"
    token_seed = f"{run_id}:{batch_id}:{attempt_id}:{rev}"
    token_a = derive_canary_token(f"{token_seed}:A", length=8)
    token_b = derive_canary_token(f"{token_seed}:B", length=8)
    expected_ans = f"{token_a}|{token_b}"

    w, h = 320, 96
    resolved_font = get_canary_font_path(font_path)
    buf_a = render_canary_frame(token_a, font_path=resolved_font, width=w, height=h, draw_shapes=False, fontsize=32)
    buf_b = render_canary_frame(token_b, font_path=resolved_font, width=w, height=h, draw_shapes=False, fontsize=32)

    # 4 frames total: first 2 frames token A (seconds 0-1), last 2 frames token B (seconds 2-3)
    raw_video = (buf_a * 2) + (buf_b * 2)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    if not os.path.exists(ffmpeg_bin):
        raise JudgeTransportError("ffmpeg executable not found for attention video generation")

    cmd = [
        ffmpeg_bin,
        "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{w}x{h}",
        "-pix_fmt", "rgb24",
        "-r", "1",
        "-i", "-",
        "-c:v", "libx264",
        "-crf", "0",
        "-pix_fmt", "yuv420p",
        "-r", "1",
        "-t", "4",
        str(out_path),
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=raw_video,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=30,
        )
    except subprocess.SubprocessError as exc:
        raise JudgeTransportError(f"Failed to encode attention video: {exc}") from exc

    sha256 = compute_file_sha256(out_path)
    return {
        "token_a": token_a,
        "token_b": token_b,
        "expected": expected_ans,
        "path": out_path,
        "sha256": sha256,
        "kind": "mp4",
        "duration_ms": 4000,
        "frame_count": 4,
    }


class DualKeyDict(dict):
    """Dictionary supporting lookup by either string or integer keys."""

    def __getitem__(self, key: Any) -> Any:
        if key in self:
            return super().__getitem__(key)
        if isinstance(key, int) and str(key) in self:
            return super().__getitem__(str(key))
        if isinstance(key, str) and key.isdigit():
            int_k = int(key)
            if int_k in self:
                return super().__getitem__(int_k)
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


def prepare_judge_mp4(
    src_file: Path,
    dest_path: Path,
    cl: dict[str, Any],
    frames_by_eid: dict[str, Any],
    points_by_pid: dict[str, Any],
    max_frames: int = 20,
) -> tuple[str, int, int, DualKeyDict]:
    """Re-encode render MP4 clip into 1 fps judge copy with <= max_frames uniformly sampled (L-7).

    Returns:
      (dest_sha, duration_ms, frame_count, source_map)
    """
    ffmpeg_bin = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    if not os.path.exists(ffmpeg_bin):
        raise JudgeTransportError("ffmpeg executable not found for judge MP4 re-encoding")

    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        # 1. Extract frames from source clip
        extract_cmd = [
            ffmpeg_bin,
            "-v", "error",
            "-i", str(src_file),
            str(tmp_dir / "src_%06d.png"),
        ]
        try:
            subprocess.run(extract_cmd, check=True, timeout=30)
        except subprocess.SubprocessError as exc:
            raise JudgeValidationError(f"Failed to extract frames from {src_file.name}: {exc}", code="clip_decode_error") from exc

        frame_files = sorted(tmp_dir.glob("src_*.png"))
        N = len(frame_files)
        if N == 0:
            raise JudgeValidationError(f"Source clip {src_file.name} yielded 0 decodable frames", code="empty_clip_frames")

        # 2. Subsample uniformly (always keep first and last), at most max_frames
        target = min(max_frames, N)
        if N <= max_frames or target <= 1:
            selected_indices = list(range(target))
        else:
            selected_indices = [round(i * (N - 1) / (target - 1)) for i in range(target)]

        stage_dir = tmp_dir / "stage"
        stage_dir.mkdir(parents=True, exist_ok=True)
        for new_idx, old_idx in enumerate(selected_indices):
            shutil.copy2(frame_files[old_idx], stage_dir / f"f_{new_idx:06d}.png")

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if dest_path.exists():
            dest_path.unlink()

        encode_cmd = [
            ffmpeg_bin,
            "-y",
            "-v", "error",
            "-framerate", "1",
            "-start_number", "0",
            "-i", str(stage_dir / "f_%06d.png"),
            "-frames:v", str(target),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", "1",
            str(dest_path),
        ]
        try:
            subprocess.run(encode_cmd, check=True, timeout=30)
        except subprocess.SubprocessError as exc:
            raise JudgeTransportError(f"Failed to encode judge MP4 {dest_path.name}: {exc}") from exc

        dest_sha = compute_file_sha256(dest_path)
        validate_mp4_duration(dest_path, min_duration_s=1.0)

        # 3. Build source map for each second k with exact witnesses (A-P3-8)
        cl_map = cl.get("frame_to_render_index") or cl.get("render_index_map") or {}
        cl_frame_ids = cl.get("frame_ids", [])
        if len(cl_frame_ids) != N:
            raise JudgeValidationError(
                f"Clip frame count mismatch: decoded {N} frames from {dest_path.name} but clip frame_ids has {len(cl_frame_ids)}"
            )

        sec_map = DualKeyDict()
        for k, src_idx in enumerate(selected_indices):
            if src_idx >= len(cl_frame_ids):
                raise JudgeValidationError(
                    f"Selected index {src_idx} out of range for clip frame_ids (len {len(cl_frame_ids)})"
                )
            fid = cl_frame_ids[src_idx]
            fr = frames_by_eid.get(fid)
            render_idx = fr.get("render_index") if fr else None
            tick = fr.get("tick") if fr else None

            if render_idx is None and cl_map:
                render_idx = cl_map.get(str(src_idx), cl_map.get(src_idx))
            if tick is None and cl.get("frame_to_tick"):
                tick = cl["frame_to_tick"].get(str(src_idx), cl["frame_to_tick"].get(src_idx))

            if render_idx is None or tick is None:
                raise JudgeValidationError(
                    f"Missing exact source frame/render/tick witness for decoded frame {src_idx} (evidence_id {fid!r}): render_index={render_idx}, tick={tick}"
                )

            sec_map[str(k)] = {
                "render_index": int(render_idx),
                "tick": int(tick),
            }

        duration_ms = target * 1000
        return dest_sha, duration_ms, target, sec_map


# ---------------------------------------------------------------------------
# Prompt Template Loading & Expansion
# ---------------------------------------------------------------------------

def load_judge_prompt_template() -> str:
    """Load exact versioned judge prompt template v1."""
    tpl_path = Path(__file__).parent / "judge_prompt_template_v1.txt"
    if not tpl_path.is_file():
        raise JudgeValidationError(f"Missing judge prompt template: {tpl_path}")
    with open(tpl_path, "r", encoding="utf-8") as f:
        return f.read()


def load_schema(name: str) -> dict[str, Any]:
    """Load a schema JSON file from the blender_visual_qa/schemas directory."""
    path = Path(__file__).parent.parent / "schemas" / name.replace(".schema.json", ".v1.schema.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def probe_mp4_duration(path: Path) -> float:
    """Probe MP4 duration in seconds using ffprobe.

    Raises JudgeValidationError if ffprobe fails or duration cannot be read.
    """
    ffprobe_bin = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration:stream=duration",
        "-of", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=15)
        data = json.loads(proc.stdout.decode("utf-8"))
        fmt_dur = data.get("format", {}).get("duration")
        if fmt_dur is not None:
            return float(fmt_dur)
        for s in data.get("streams", []):
            s_dur = s.get("duration")
            if s_dur is not None:
                return float(s_dur)
        raise JudgeValidationError(f"Could not determine duration for MP4 media {path.name}", code="ffprobe_no_duration")
    except subprocess.SubprocessError as exc:
        raise JudgeValidationError(f"ffprobe failed for {path.name}: {exc}", code="ffprobe_error") from exc
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise JudgeValidationError(f"Failed to parse ffprobe duration for {path.name}: {exc}", code="ffprobe_parse_error") from exc


def validate_mp4_duration(path: Path, min_duration_s: float = 1.0) -> float:
    """Validate that an MP4 file has duration >= min_duration_s using ffprobe.

    Raises JudgeValidationError if duration < min_duration_s.
    """
    dur = probe_mp4_duration(path)
    if dur < min_duration_s:
        raise JudgeValidationError(
            f"Pre-dispatch validation rejected MP4 media {path.name}: duration {dur:.3f}s is less than {min_duration_s:.1f}s required",
            code="invalid_media_duration",
        )
    return dur


def validate_media_pre_dispatch(media_entries: list[dict[str, Any]], run_dir: Path, min_duration_s: float = 1.0) -> None:
    """Validate all MP4 media in media entries before dispatching to worker."""
    for m in media_entries:
        if m.get("kind") == "mp4":
            m_path = run_dir / m["path"]
            validate_mp4_duration(m_path, min_duration_s=min_duration_s)


# ---------------------------------------------------------------------------
# Batch Building (D-S3-3, D-S3-4, D-S3-5)
# ---------------------------------------------------------------------------

def _categorize_check(ch: dict[str, Any]) -> str:
    """Determine batch ID (q01..q04) for a checklist check."""
    cid = ch["id"].lower()
    desc = ch.get("description", "").lower()
    points = [p.lower() for p in ch.get("point_ids", [])]
    text = f"{cid} {desc} {' '.join(points)}"

    if any(k in text for k in ("pause", "paused")):
        return "q02"
    if any(k in text for k in ("card", "levelup", "level_up", "offer")):
        return "q03"
    if any(k in text for k in ("fleet", "formation", "camera", "tank", "healer", "drift", "frozen", "moving", "motion")):
        return "q04"
    return "q01"


def _build_attempt_for_batch(
    capture: dict[str, Any],
    checklist: dict[str, Any],
    run_dir: Path,
    batch_id: str,
    b_checks: list[dict[str, Any]],
    attempt_id: str,
    is_retry: bool = False,
    abstaining_check_ids: set[str] | None = None,
    allowed_source_ids: set[str] | None = None,
) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    raw_run_id = capture.get("run_id")
    forbidden_label_words = ("clean", "mutant", "control", "hidden", "expected_pass", "expected_fail")
    has_defect_label = (raw_run_id and any(w in str(raw_run_id).lower() for w in forbidden_label_words)) or any(w in run_dir.name.lower() for w in forbidden_label_words)
    if has_defect_label:
        cap_bytes = json.dumps(capture, sort_keys=True).encode("utf-8")
        run_id = f"run_{hashlib.sha256(cap_bytes).hexdigest()[:16]}"
        needs_neutral_staging = True
    else:
        run_id = str(raw_run_id) if raw_run_id else run_dir.name
        needs_neutral_staging = False
    checklist_sha256 = compute_canonical_sha256(checklist)

    # Map capture frames & clips by evidence_id / point_id
    frames_by_eid: dict[str, dict[str, Any]] = {}
    for f in capture.get("frames", []):
        frames_by_eid[f["evidence_id"]] = f

    clips_by_pid: dict[str, dict[str, Any]] = {}
    clips_by_eid: dict[str, dict[str, Any]] = {}
    for cl in capture.get("clips", []):
        clips_by_pid[cl["point_id"]] = cl
        clips_by_eid[cl["evidence_id"]] = cl

    points_by_pid: dict[str, dict[str, Any]] = {}
    for pt in capture.get("points", []):
        points_by_pid[pt["point_id"]] = pt

    judge_dir = run_dir / "judge"
    judge_dir.mkdir(parents=True, exist_ok=True)

    prompt_tpl = load_judge_prompt_template()
    output_schema_obj = load_schema("judge-output.schema.json")

    attempt_dir = judge_dir / f"attempt_{attempt_id}"
    media_dir = attempt_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    # Collect relevant render evidence
    render_media_entries: list[dict[str, Any]] = []
    check_entries: list[dict[str, Any]] = []
    panel_map: dict[str, Any] = {}
    source_map: dict[str, Any] = {}
    copied_sources: set[str] = set()
    copied_source_to_eid: dict[str, str] = {}

    for ch in b_checks:
        cid = ch["id"]
        j_spec = ch.get("judge") or {}
        req = j_spec.get("rubric", ch.get("description", ""))
        p_anchor = j_spec.get("pass_anchor", "Visual elements conform to expected behavior.")
        f_anchor = j_spec.get("fail_anchor", "Visual elements exhibit defects or abnormalities.")

        ch_media = str(ch.get("media", "")).lower()
        pids = ch.get("point_ids", [])

        # A-P3-4: Verify every declared applicable point exists and is complete
        has_all_declared_points = True
        for pid in pids:
            pt = points_by_pid.get(pid)
            if not pt:
                has_all_declared_points = False
                break
            if ch_media not in ("clip", "mp4", "video") and not (pt.get("selected_frame_ids") and any(sf in frames_by_eid for sf in pt["selected_frame_ids"])):
                if ch_media in ("frame", "png"):
                    has_all_declared_points = False
                    break
            if ch_media not in ("frame", "png") and not (pt.get("clip_id") and (pt["clip_id"] in clips_by_eid or pt["clip_id"] in clips_by_pid)):
                if ch_media in ("clip", "mp4", "video"):
                    has_all_declared_points = False
                    break

        if not has_all_declared_points:
            # Check is incomplete due to missing/incomplete point: do not dispatch, do not fallback
            continue

        ch_evidence_ids: list[str] = []
        for pid in pids:
            pt = points_by_pid.get(pid)
            if pt and pt.get("selected_frame_ids") and ch_media not in ("clip", "mp4", "video"):
                raw_frame_ids = pt["selected_frame_ids"]
                if is_retry and len(raw_frame_ids) > 1 and (abstaining_check_ids is None or cid in abstaining_check_ids):
                    sf_candidates = list(raw_frame_ids[1:]) + list(raw_frame_ids[:1])
                else:
                    sf_candidates = list(raw_frame_ids)

                for sf_id in sf_candidates:
                    if allowed_source_ids is not None and sf_id not in allowed_source_ids:
                        continue
                    fr = frames_by_eid.get(sf_id)
                    if fr and sf_id not in copied_sources and len(render_media_entries) < MAX_GAMEPLAY_MEDIA_PER_BATCH:
                        src_rel = fr["file"]["path"]
                        src_file = (run_dir / src_rel).resolve()
                        try:
                            src_file.relative_to(run_dir)
                        except ValueError:
                            raise JudgeValidationError(f"Symlink escape: {src_file} escapes {run_dir}")
                        if not src_file.is_file():
                            continue
                        file_sha = fr["file"]["sha256"]
                        actual_src_sha = compute_file_sha256(src_file)
                        if actual_src_sha != file_sha:
                            raise JudgeValidationError(
                                f"Pre-copy source mutation for frame {sf_id}: {actual_src_sha} != declared {file_sha}"
                            )

                        opaque_id = hashlib.sha256(f"{cid}:{sf_id}:{file_sha}:{attempt_id}".encode("utf-8")).hexdigest()[:12]
                        dest_name = f"m_{opaque_id}.png"
                        dest_path = media_dir / dest_name
                        if dest_path.exists():
                            try:
                                os.chmod(dest_path, 0o666)
                                dest_path.unlink()
                            except OSError:
                                pass
                        shutil.copy2(src_file, dest_path)
                        dest_sha = compute_file_sha256(dest_path)
                        if dest_sha != actual_src_sha:
                            raise JudgeValidationError(f"Copy corruption for {dest_name}: {dest_sha} != {actual_src_sha}")
                        try:
                            os.chmod(dest_path, 0o444)
                        except OSError:
                            pass

                        neutral_eid = f"ev_{opaque_id}"
                        copied_sources.add(sf_id)
                        copied_source_to_eid[sf_id] = neutral_eid
                        source_map[neutral_eid] = sf_id
                        ch_evidence_ids.append(neutral_eid)

                        rel_path = str(dest_path.relative_to(run_dir))
                        render_media_entries.append({
                            "evidence_id": neutral_eid,
                            "source_evidence_ids": [sf_id],
                            "path": rel_path,
                            "sha256": dest_sha,
                            "kind": "png",
                            "role": "render",
                            "tick_start": fr.get("tick", 0),
                            "tick_end": fr.get("tick", 0),
                            "duration_ms": 0,
                            "frame_count": 1,
                        })
                    elif fr and sf_id in copied_sources:
                        existing_eid = copied_source_to_eid.get(sf_id)
                        if existing_eid and existing_eid not in ch_evidence_ids:
                            ch_evidence_ids.append(existing_eid)

            if pt and pt.get("clip_id") and ch_media not in ("frame", "png"):
                cid_pt = pt["clip_id"]
                if allowed_source_ids is not None and cid_pt not in allowed_source_ids:
                    pass
                else:
                    cl = clips_by_eid.get(cid_pt) or clips_by_pid.get(pid)
                    if cl and cid_pt not in copied_sources and len(render_media_entries) < MAX_GAMEPLAY_MEDIA_PER_BATCH:
                        src_rel = cl["file"]["path"]
                        src_file = (run_dir / src_rel).resolve()
                        try:
                            src_file.relative_to(run_dir)
                        except ValueError:
                            raise JudgeValidationError(f"Symlink escape: {src_file} escapes {run_dir}")
                        if not src_file.is_file():
                            continue
                        file_sha = cl["file"]["sha256"]
                        actual_src_sha = compute_file_sha256(src_file)
                        if actual_src_sha != file_sha:
                            raise JudgeValidationError(
                                f"Pre-copy source mutation for clip {cid_pt}: {actual_src_sha} != declared {file_sha}"
                            )

                        opaque_id = hashlib.sha256(f"{cid}:{cid_pt}:{file_sha}:{attempt_id}".encode("utf-8")).hexdigest()[:12]
                        dest_name = f"m_{opaque_id}.mp4"
                        dest_path = media_dir / dest_name
                        if dest_path.exists():
                            try:
                                os.chmod(dest_path, 0o666)
                                dest_path.unlink()
                            except OSError:
                                pass
                        dest_sha, duration_ms, frame_count, clip_source_map = prepare_judge_mp4(
                            src_file=src_file,
                            dest_path=dest_path,
                            cl=cl,
                            frames_by_eid=frames_by_eid,
                            points_by_pid=points_by_pid,
                            max_frames=20,
                        )
                        try:
                            os.chmod(dest_path, 0o444)
                        except OSError:
                            pass

                        neutral_eid = f"ev_{opaque_id}"
                        copied_sources.add(cid_pt)
                        copied_source_to_eid[cid_pt] = neutral_eid
                        source_map[neutral_eid] = clip_source_map
                        ch_evidence_ids.append(neutral_eid)

                        rel_path = str(dest_path.relative_to(run_dir))
                        render_media_entries.append({
                            "evidence_id": neutral_eid,
                            "source_evidence_ids": [cid_pt],
                            "path": rel_path,
                            "sha256": dest_sha,
                            "kind": "mp4",
                            "role": "render",
                            "tick_start": cl.get("tick_start", 0),
                            "tick_end": cl.get("tick_end", 0),
                            "duration_ms": duration_ms,
                            "frame_count": frame_count,
                            "source_map": clip_source_map,
                        })
                    elif cl and cid_pt in copied_sources:
                        existing_eid = copied_source_to_eid.get(cid_pt)
                        if existing_eid and existing_eid not in ch_evidence_ids:
                            ch_evidence_ids.append(existing_eid)

        if ch_evidence_ids:
            check_entries.append({
                "check_id": cid,
                "requirement": req[:1200],
                "pass_anchor": p_anchor[:600],
                "fail_anchor": f_anchor[:600],
                "evidence_ids": list(dict.fromkeys(ch_evidence_ids)),
                "evidence_requirement": checklist.get("judge_policy", {}).get("evidence_requirements", {}).get(cid, "scene"),
            })

    # Attention media
    att_png_file = media_dir / f"m_att_png_{attempt_id}.png"
    att_vid_file = media_dir / f"m_att_vid_{attempt_id}.mp4"
    for f_p in (att_png_file, att_vid_file):
        if f_p.exists():
            try:
                os.chmod(f_p, 0o666)
                f_p.unlink()
            except OSError:
                pass
    att_png_info = create_attention_png(run_id, batch_id, attempt_id, att_png_file)
    att_vid_info = create_attention_video(run_id, batch_id, attempt_id, att_vid_file)
    validate_mp4_duration(att_vid_file, min_duration_s=1.0)

    att_png_eid = f"eid_att_png_{attempt_id}"
    att_vid_eid = f"eid_att_vid_{attempt_id}"

    media_entries = list(render_media_entries)
    media_entries.append({
        "evidence_id": att_png_eid,
        "source_evidence_ids": [],
        "path": str(att_png_file.relative_to(run_dir)),
        "sha256": att_png_info["sha256"],
        "kind": "png",
        "role": "attention",
        "tick_start": None,
        "tick_end": None,
        "duration_ms": 0,
        "frame_count": 1,
    })
    media_entries.append({
        "evidence_id": att_vid_eid,
        "source_evidence_ids": [],
        "path": str(att_vid_file.relative_to(run_dir)),
        "sha256": att_vid_info["sha256"],
        "kind": "mp4",
        "role": "attention",
        "tick_start": None,
        "tick_end": None,
        "duration_ms": 4000,
        "frame_count": 4,
    })

    attention_questions = [
        {
            "question_id": "attention_text",
            "evidence_id": att_png_eid,
            "question": "Transcribe the printed token exactly",
        },
        {
            "question_id": "attention_absent",
            "evidence_id": att_png_eid,
            "question": "Is a triangle visible? Answer YES, NO, or CANNOT_SEE.",
        },
        {
            "question_id": "attention_video",
            "evidence_id": att_vid_eid,
            "question": "Transcribe the first and second printed tokens in temporal order, separated by |. If unavailable answer CANNOT_SEE.",
        },
    ]

    expectations_obj = {
        "schema": SCHEMA_JUDGE_EXPECTATIONS,
        "attention": {
            "attention_text": att_png_info["token"],
            "attention_absent": "NO",
            "attention_video": att_vid_info["expected"],
        },
        "panel_map": panel_map,
        "source_map": source_map,
    }

    input_obj = {
        "schema": SCHEMA_JUDGE_INPUT,
        "run_id": run_id,
        "batch_id": batch_id,
        "attempt_id": attempt_id,
        "model_id": DEFAULT_MODEL_ID,
        "effort": DEFAULT_EFFORT,
        "checklist_sha256": checklist_sha256,
        "checks": check_entries,
        "media": media_entries,
        "attention_questions": attention_questions,
        "source_map": {
            m["evidence_id"]: m["source_map"]
            for m in media_entries
            if "source_map" in m
        },
    }
    contracts.validate_schema(input_obj, "judge-input")

    input_file = attempt_dir / "input.json"
    if input_file.exists():
        try:
            os.chmod(input_file, 0o666)
            input_file.unlink()
        except OSError:
            pass
    with open(input_file, "w", encoding="utf-8") as f:
        json.dump(input_obj, f, indent=2, sort_keys=True, ensure_ascii=False)
    input_sha256 = compute_file_sha256(input_file)

    media_abs_list = []
    if needs_neutral_staging:
        neutral_stage_base = Path(tempfile.gettempdir()) / "vqa_neutral_stage"
        opaque_attempt = hashlib.sha256(f"{run_id}:{batch_id}:{attempt_id}".encode("utf-8")).hexdigest()[:16]
        neutral_media_dir = neutral_stage_base / opaque_attempt / "media"
        neutral_media_dir.mkdir(parents=True, exist_ok=True)

        for m in media_entries:
            dest_p = (run_dir / m["path"]).resolve()
            neutral_dest = neutral_media_dir / dest_p.name
            if neutral_dest.is_symlink() or neutral_dest.exists():
                try:
                    neutral_dest.unlink()
                except OSError:
                    pass
            try:
                neutral_dest.symlink_to(dest_p)
            except OSError:
                try:
                    shutil.copy2(dest_p, neutral_dest)
                except OSError:
                    neutral_dest = dest_p

            m_copy = dict(m)
            m_copy["path"] = str(neutral_dest)
            media_abs_list.append(m_copy)
    else:
        for m in media_entries:
            dest_p = (run_dir / m["path"]).resolve()
            m_copy = dict(m)
            m_copy["path"] = str(dest_p)
            media_abs_list.append(m_copy)

    prompt_expanded = (
        prompt_tpl.replace("RUN_ID", run_id)
        .replace("BATCH_ID", batch_id)
        .replace("ATTEMPT_ID", attempt_id)
        .replace("INPUT_SHA256", input_sha256)
        .replace("MEDIA_JSON", json.dumps(media_abs_list, indent=2, ensure_ascii=False))
        .replace("CHECKS_JSON", json.dumps(check_entries, indent=2, ensure_ascii=False))
        .replace("ATTENTION_QUESTIONS_JSON", json.dumps(attention_questions, indent=2, ensure_ascii=False))
        .replace("OUTPUT_SCHEMA_JSON", json.dumps(output_schema_obj, indent=2, ensure_ascii=False))
    )

    checks_dump = json.dumps(check_entries).lower()
    media_dump = json.dumps(media_entries).lower()
    forbidden_terms = ["mutant", "expectations.json", "telemetry"]
    for term in forbidden_terms:
        if term in checks_dump or term in media_dump:
            raise JudgeValidationError(
                f"Forbidden term {term!r} detected in checks or media",
                code="prompt_independence_violation",
            )

    prompt_sha256 = hashlib.sha256(prompt_expanded.encode("utf-8")).hexdigest()
    final_prompt = prompt_expanded.replace("__PROMPT_SHA256__", prompt_sha256)

    prompt_file = attempt_dir / "prompt.md"
    if prompt_file.exists():
        try:
            os.chmod(prompt_file, 0o666)
            prompt_file.unlink()
        except OSError:
            pass
    with open(prompt_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(final_prompt)
    prompt_file_sha = compute_file_sha256(prompt_file)

    # Bind input and prompt sha256 to expectations (A-P3-1)
    expectations_obj["input_sha256"] = input_sha256
    expectations_obj["input_canonical_sha256"] = compute_canonical_sha256(input_obj)
    expectations_obj["prompt_sha256"] = prompt_sha256
    expectations_obj["prompt_file_sha256"] = prompt_file_sha
    exp_file = attempt_dir / "expectations.json"
    if exp_file.exists():
        try:
            os.chmod(exp_file, 0o666)
            exp_file.unlink()
        except OSError:
            pass
    with open(exp_file, "w", encoding="utf-8") as f:
        json.dump(expectations_obj, f, indent=2, sort_keys=True)
    try:
        os.chmod(exp_file, 0o600)
    except OSError:
        pass

    return {
        "batch_id": batch_id,
        "attempt_id": attempt_id,
        "attempt_dir": attempt_dir,
        "input_file": input_file,
        "input_obj": input_obj,
        "input_sha256": input_sha256,
        "prompt_file": prompt_file,
        "prompt_sha256": prompt_sha256,
        "expectations_file": exp_file,
        "expectations_obj": expectations_obj,
        "media_entries": media_entries,
        "media": media_entries,
        "checks": check_entries,
        "b_checks": b_checks,
        "allowed_source_ids": allowed_source_ids,
    }


def build_batches(capture: dict[str, Any], checklist: dict[str, Any], run_dir: Path) -> list[dict[str, Any]]:
    """Build input.json, prompt.md, held expectations, and media copies for each batch.

    Writes:
      <run-dir>/judge/attempt_<attempt_id>/input.json
      <run-dir>/judge/attempt_<attempt_id>/prompt.md
      <run-dir>/judge/attempt_<attempt_id>/expectations.json
      <run-dir>/judge/attempt_<attempt_id>/media/m_...
    Validates hashes, bounds, independence and schemas.
    """
    contracts.validate_schema(capture, "capture")
    contracts.validate_schema(checklist, "checklist")

    # Validate canary font existence before dispatching batches (L-5)
    get_canary_font_path()

    # Pre-dispatch validation rejects MP4 media < 1.0s via ffprobe (L-1)
    resolved_run_dir = Path(run_dir).resolve()
    for cl in capture.get("clips", []):
        c_path = cl.get("file", {}).get("path")
        if c_path:
            full_c = resolved_run_dir / c_path
            if full_c.is_file():
                validate_mp4_duration(full_c, min_duration_s=1.0)

    all_checks = checklist.get("checks", [])
    judge_checks = [ch for ch in all_checks if ch.get("judge") is not None]
    if not judge_checks:
        return []

    # Map capture frames & clips by evidence_id / point_id
    frames_by_eid = {f["evidence_id"]: f for f in capture.get("frames", [])}
    clips_by_pid = {cl["point_id"]: cl for cl in capture.get("clips", [])}
    clips_by_eid = {cl["evidence_id"]: cl for cl in capture.get("clips", [])}
    points_by_pid = {pt["point_id"]: pt for pt in capture.get("points", [])}

    batch_buckets: dict[str, list[dict[str, Any]]] = {"q01": [], "q02": [], "q03": [], "q04": []}
    for ch in judge_checks:
        b_id = _categorize_check(ch)
        batch_buckets[b_id].append(ch)

    batches: list[dict[str, Any]] = []
    for base_id in sorted(batch_buckets.keys()):
        b_checks = batch_buckets[base_id]
        if not b_checks:
            continue

        # Collect unique required render sources for b_checks with strict point validation (A-P3-4)
        unique_sources: list[str] = []
        check_sources: dict[str, list[str]] = {}
        valid_b_checks: list[dict[str, Any]] = []
        for ch in b_checks:
            cid = ch["id"]
            ch_media = str(ch.get("media", "")).lower()
            pids = ch.get("point_ids", [])
            has_all_declared_points = True
            for pid in pids:
                pt = points_by_pid.get(pid)
                if not pt:
                    has_all_declared_points = False
                    break
                if ch_media not in ("clip", "mp4", "video") and not (pt.get("selected_frame_ids") and any(sf in frames_by_eid for sf in pt["selected_frame_ids"])):
                    if ch_media in ("frame", "png"):
                        has_all_declared_points = False
                        break
                if ch_media not in ("frame", "png") and not (pt.get("clip_id") and (pt["clip_id"] in clips_by_eid or pt["clip_id"] in clips_by_pid)):
                    if ch_media in ("clip", "mp4", "video"):
                        has_all_declared_points = False
                        break

            if not has_all_declared_points:
                continue

            c_srcs: list[str] = []
            for pid in pids:
                pt = points_by_pid.get(pid)
                if pt and pt.get("selected_frame_ids") and ch_media not in ("clip", "mp4", "video"):
                    for sf_id in pt["selected_frame_ids"]:
                        if sf_id in frames_by_eid:
                            c_srcs.append(sf_id)
                            if sf_id not in unique_sources:
                                unique_sources.append(sf_id)
                if pt and pt.get("clip_id") and ch_media not in ("frame", "png"):
                    cid_pt = pt["clip_id"]
                    if cid_pt in clips_by_eid or cid_pt in clips_by_pid:
                        c_srcs.append(cid_pt)
                        if cid_pt not in unique_sources:
                            unique_sources.append(cid_pt)

            if c_srcs:
                check_sources[cid] = c_srcs
                valid_b_checks.append(ch)

        b_checks = valid_b_checks
        if not b_checks:
            continue

        if len(unique_sources) > MAX_GAMEPLAY_MEDIA_PER_BATCH:
            # L-6: Reduce media per batch: at most 3 render media items per batch (split batches instead)
            chunks = [
                unique_sources[i : i + MAX_GAMEPLAY_MEDIA_PER_BATCH]
                for i in range(0, len(unique_sources), MAX_GAMEPLAY_MEDIA_PER_BATCH)
            ]
            for idx, chunk in enumerate(chunks):
                sub_batch_id = f"{base_id}_{idx + 1}"
                sub_attempt_id = f"{sub_batch_id}_a1"
                chunk_set = set(chunk)
                sub_checks = [ch for ch in b_checks if any(s in chunk_set for s in check_sources.get(ch["id"], []))]
                if not sub_checks:
                    sub_checks = b_checks
                attempt_data = _build_attempt_for_batch(
                    capture=capture,
                    checklist=checklist,
                    run_dir=run_dir,
                    batch_id=sub_batch_id,
                    b_checks=sub_checks,
                    attempt_id=sub_attempt_id,
                    is_retry=False,
                    allowed_source_ids=chunk_set,
                )
                batches.append(attempt_data)
        else:
            attempt_id = f"{base_id}_a1"
            attempt_data = _build_attempt_for_batch(
                capture=capture,
                checklist=checklist,
                run_dir=run_dir,
                batch_id=base_id,
                b_checks=b_checks,
                attempt_id=attempt_id,
                is_retry=False,
            )
            batches.append(attempt_data)

    return batches


# ---------------------------------------------------------------------------
# Validation (D-S3-5, D-S3-6)
# ---------------------------------------------------------------------------

# @anchor:bv.judge.validate
def validate_response(input_obj: dict[str, Any], output_obj: dict[str, Any], expectations: dict[str, Any]) -> dict[str, Any]:
    """Pure validation and normalization of model judge output against input and held expectations.

    Returns:
      {"valid": bool, "errors": list[str], "notes": list[str], "results": list[dict], "outcome": str}
    """
    errors: list[str] = []
    notes: list[str] = []
    results: list[dict[str, Any]] = []

    # Geometry is observation-local, including values rejected by the wire schema.
    dropped_bbox_checks: set[str] = set()
    # Missing MP4 timestamps have the same observation-local semantics as null.
    output_obj = copy.deepcopy(output_obj)
    mp4_ids = {m.get("evidence_id") for m in input_obj.get("media", []) if m.get("kind") == "mp4"}
    for result in output_obj.get("results", []) if isinstance(output_obj, dict) else []:
        if isinstance(result, dict) and isinstance(result.get("observations"), list):
            retained = []
            for observation in result["observations"]:
                if isinstance(observation, dict):
                    bbox = observation.get("bbox_norm")
                    invalid_bbox = (bbox is not None and not (
                        isinstance(bbox, list) and len(bbox) == 4
                        and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                and 0 <= v <= 1 and math.isfinite(v) for v in bbox)
                        and bbox[0] < bbox[2] and bbox[1] < bbox[3]))
                    missing_ocr_bbox = observation.get("kind") == "ocr" and bbox is None
                    if invalid_bbox or missing_ocr_bbox:
                        cid = result.get("check_id")
                        dropped_bbox_checks.add(cid)
                        notes.append(f"dropped: invalid bbox_norm for evidence {observation.get('evidence_id')} in check {cid}: {bbox!r}")
                        continue
                    if observation.get("evidence_id") in mp4_ids:
                        observation.setdefault("time_ms", None)
                retained.append(observation)
            result["observations"] = retained

    # 1. Schema check
    try:
        contracts.validate_schema(output_obj, "judge-output")
    except Exception as exc:
        errors.append(f"Output schema validation failure: {exc}")
        return {"valid": False, "errors": errors, "notes": [], "results": []}

    # 2. Bind identities
    if output_obj.get("run_id") != input_obj.get("run_id"):
        errors.append(f"run_id mismatch: {output_obj.get('run_id')} != {input_obj.get('run_id')}")
    if output_obj.get("batch_id") != input_obj.get("batch_id"):
        errors.append(f"batch_id mismatch: {output_obj.get('batch_id')} != {input_obj.get('batch_id')}")
    if output_obj.get("attempt_id") != input_obj.get("attempt_id"):
        errors.append(f"attempt_id mismatch: {output_obj.get('attempt_id')} != {input_obj.get('attempt_id')}")
    if output_obj.get("model_id") != DEFAULT_MODEL_ID:
        errors.append(f"model_id mismatch: {output_obj.get('model_id')} != {DEFAULT_MODEL_ID}")

    valid_input_shas = set()
    if expectations.get("input_sha256"):
        valid_input_shas.add(expectations["input_sha256"])
    if expectations.get("input_canonical_sha256"):
        valid_input_shas.add(expectations["input_canonical_sha256"])
    valid_input_shas.add(compute_canonical_sha256(input_obj))

    out_input_sha = output_obj.get("input_sha256")
    if not out_input_sha or out_input_sha not in valid_input_shas:
        errors.append(f"input_sha256 mismatch: {out_input_sha} not in {sorted(valid_input_shas)}")

    exp_prompt_sha = expectations.get("prompt_sha256")
    out_prompt_sha = output_obj.get("prompt_sha256")
    if exp_prompt_sha is not None:
        if not out_prompt_sha or out_prompt_sha != exp_prompt_sha:
            errors.append(f"prompt_sha256 mismatch: {out_prompt_sha} != {exp_prompt_sha}")
    elif not out_prompt_sha or len(out_prompt_sha) != 64:
        errors.append(f"Invalid prompt_sha256: {out_prompt_sha}")

    # 3. Media seen validation
    valid_media_ids = {m["evidence_id"] for m in input_obj.get("media", [])}
    media_by_id = {m["evidence_id"]: m for m in input_obj.get("media", [])}
    media_seen = set(output_obj.get("media_seen", []))
    unknown_seen = media_seen - valid_media_ids
    if unknown_seen:
        errors.append(f"Unknown evidence ids in media_seen: {sorted(unknown_seen)}")

    # 4. Attention questions validation (ASCII whitespace only per A-P3-3)
    exp_att = expectations.get("attention", {})
    out_att = output_obj.get("attention_answers", {})

    ascii_ws = " \t\n\r\v\f"
    ans_text = str(out_att.get("attention_text", "")).strip(ascii_ws)
    ans_absent = str(out_att.get("attention_absent", "")).strip(ascii_ws)
    ans_video = str(out_att.get("attention_video", "")).strip(ascii_ws)

    exp_text = str(exp_att.get("attention_text", "")).strip(ascii_ws)
    exp_absent = str(exp_att.get("attention_absent", "")).strip(ascii_ws)
    exp_video = str(exp_att.get("attention_video", "")).strip(ascii_ws)

    raw_results = output_obj.get("results", [])
    all_att_cannot_see = (
        ans_text == "CANNOT_SEE" and
        ans_absent == "CANNOT_SEE" and
        ans_video == "CANNOT_SEE"
    )
    all_results_cannot_see = (
        bool(raw_results) and
        all(
            isinstance(r, dict)
            and r.get("verdict") == "CANNOT_SEE"
            and float(r.get("confidence", 0.0) or 0.0) == 0.0
            and not r.get("observations")
            for r in raw_results
        )
    )

    canaries_passed = True
    is_honest_unavailable = all_att_cannot_see and all_results_cannot_see

    if all_att_cannot_see:
        if not all_results_cannot_see:
            errors.append(
                "Attention answers reported CANNOT_SEE but check results contain non-CANNOT_SEE or non-zero confidence/observations"
            )
            canaries_passed = False
    else:
        if ans_text != exp_text:
            errors.append(f"Attention text canary failed: {ans_text!r} != {exp_text!r}")
            canaries_passed = False
        if ans_absent != exp_absent:
            errors.append(f"Attention absent canary failed (invented shape): {ans_absent!r} != {exp_absent!r}")
            canaries_passed = False
        if ans_video != exp_video:
            errors.append(f"Attention video canary failed: {ans_video!r} != {exp_video!r}")
            canaries_passed = False

    # 5. Check results alignment & normalization
    input_checks = input_obj.get("checks", [])
    expected_check_ids = [c["check_id"] for c in input_checks]
    checks_by_id = {c["check_id"]: c for c in input_checks}

    out_results = output_obj.get("results", [])
    out_cids = [r.get("check_id") for r in out_results]

    if set(out_cids) != set(expected_check_ids) or len(out_cids) != len(expected_check_ids):
        errors.append(f"Check results count/id mismatch: {out_cids} vs {expected_check_ids}")

    for r in out_results:
        cid = r.get("check_id")
        if cid not in checks_by_id:
            continue
        ch = checks_by_id[cid]
        allowed_eids = set(ch.get("evidence_ids", []))

        verdict = r.get("verdict")
        conf_raw = r.get("confidence")
        reason = r.get("reason", "")
        obs = r.get("observations", [])

        # Validate confidence number
        if conf_raw is None or isinstance(conf_raw, bool) or not isinstance(conf_raw, (int, float)):
            errors.append(f"Invalid confidence type for check {cid}: {conf_raw!r}")
            continue
        if math.isnan(conf_raw) or math.isinf(conf_raw) or conf_raw < 0.0 or conf_raw > 1.0:
            errors.append(f"Invalid confidence value for check {cid}: {conf_raw}")
            continue

        conf = float(conf_raw)

        if verdict in ("PASS", "FAIL"):
            # Check requested media was seen
            for req_eid in allowed_eids:
                if req_eid not in media_seen:
                    errors.append(f"Check {cid} requires evidence {req_eid} which was not listed in media_seen")

            # Check observations
            if not obs and cid not in dropped_bbox_checks:
                errors.append(f"Check {cid} returned {verdict} but has empty observations")

            norm_obs = []
            dropped_ocr_timestamp = False
            for ob in obs:
                ob_copy = dict(ob)
                eid = ob_copy.get("evidence_id")
                if eid not in allowed_eids:
                    errors.append(f"Observation in check {cid} references disallowed evidence_id {eid}")
                m_info = media_by_id.get(eid)
                if m_info and m_info.get("role") == "attention":
                    errors.append(f"Check {cid} observation references attention media {eid}")

                if (m_info and m_info.get("kind") == "mp4" and ob_copy.get("time_ms") is None
                        and ob_copy.get("kind", "scene" if ob_copy.get("bbox_norm") is None else "ocr") == "ocr"):
                    notes.append(f"dropped: OCR observation without time_ms for MP4 evidence {eid} in check {cid}")
                    dropped_ocr_timestamp = True
                    continue

                # Bounding box & text check (A-P3-3)
                bbox = ob_copy.get("bbox_norm")

                # L-20: descriptions are scene evidence, not literal OCR.
                if "kind" not in ob_copy:
                    ob_copy["kind"] = "scene" if bbox is None else "ocr"
                    notes.append(f"normalized: legacy observation kind to {ob_copy['kind']} for evidence {eid} in check {cid}")
                if ob_copy["kind"] == "ocr" and bbox is None:
                    errors.append(f"OCR observation in check {cid} requires nonnull bbox_norm")

                # Timestamp: scene MP4 observations may describe the whole clip; OCR must locate a frame.
                t_ms = ob_copy.get("time_ms")
                if m_info and m_info.get("kind") == "png":
                    if t_ms is not None:
                        if not isinstance(t_ms, bool) and t_ms == 0:
                            ob["time_ms"] = None
                            ob_copy["time_ms"] = None
                            notes.append(f"normalized: observation time_ms 0 normalized to null for PNG evidence {eid} in check {cid}")
                        else:
                            errors.append(f"Observation for PNG evidence {eid} must have null time_ms (got {t_ms})")
                elif m_info and m_info.get("kind") == "mp4":
                    max_dur = m_info.get("duration_ms", 20000)
                    if t_ms is None:
                        if ob_copy["kind"] == "scene":
                            ob_copy["time_ms"] = None
                            notes.append(f"normalized: scene observation time_ms null for MP4 evidence {eid} in check {cid}")
                        else:
                            notes.append(f"dropped: OCR observation without time_ms for MP4 evidence {eid} in check {cid}")
                            continue
                    elif isinstance(t_ms, bool) or not isinstance(t_ms, (int, float)):
                        errors.append(f"Observation time_ms for MP4 evidence {eid} must be numeric, got {type(t_ms).__name__}")
                    elif t_ms < 0 or t_ms > max_dur:
                        errors.append(f"Observation time_ms {t_ms} exceeds duration {max_dur} for {eid}")

                norm_obs.append(ob_copy)

            insufficient_ocr = (verdict == "PASS" and ch.get("evidence_requirement") == "ocr"
                                and any(not any(o.get("evidence_id") == eid and o.get("kind") == "ocr"
                                                and o.get("bbox_norm") is not None and str(o.get("text", "")).strip()
                                                for o in norm_obs) for eid in allowed_eids))
            if not norm_obs or ((dropped_ocr_timestamp or cid in dropped_bbox_checks) and insufficient_ocr):
                results.append({"check_id": cid, "verdict": "CANNOT_SEE", "confidence": 0.0,
                                "reason": "No sufficient valid observations remain after observation validation",
                                "observations": [], "evidence_ids": list(allowed_eids)})
                continue

            # The adapter declares sufficiency; model-selected observation kind cannot
            # downgrade an OCR requirement to a generic scene assertion.
            if verdict == "PASS" and ch.get("evidence_requirement") == "ocr":
                for required_eid in allowed_eids:
                    if not any(o.get("evidence_id") == required_eid
                               and o.get("kind") == "ocr"
                               and o.get("bbox_norm") is not None
                               and str(o.get("text", "")).strip()
                               for o in norm_obs):
                        errors.append(f"Readability PASS in check {cid} requires located literal OCR for {required_eid}")

            # Confidence threshold normalization (D-S3-6)
            if conf < CONFIDENCE_MIN:
                results.append({
                    "check_id": cid,
                    "verdict": "CANNOT_SEE",
                    "confidence": 0.0,
                    "reason": f"Low confidence ({conf:.3f} < {CONFIDENCE_MIN}); normalized to CANNOT_SEE",
                    "observations": [],
                    "raw": {
                        "verdict": verdict,
                        "confidence": conf,
                        "observations": norm_obs,
                        "reason": reason,
                    },
                    "evidence_ids": list(allowed_eids),
                })
            else:
                results.append({
                    "check_id": cid,
                    "verdict": verdict,
                    "confidence": conf,
                    "reason": reason,
                    "observations": norm_obs,
                    "evidence_ids": list(allowed_eids),
                })
        elif verdict == "CANNOT_SEE":
            if conf != 0.0:
                errors.append(f"Check {cid} returned CANNOT_SEE but confidence is non-zero ({conf})")
            if obs:
                errors.append(f"Check {cid} returned CANNOT_SEE but observations list is non-empty ({obs})")
            results.append({
                "check_id": cid,
                "verdict": "CANNOT_SEE",
                "confidence": 0.0,
                "reason": reason or "Model reported CANNOT_SEE",
                "observations": [],
                "evidence_ids": list(allowed_eids),
            })
        else:
            errors.append(f"Unrecognized verdict {verdict!r} for check {cid}")

    valid = (len(errors) == 0) and canaries_passed
    outcome = "INCOMPLETE" if (valid and is_honest_unavailable) else ("VALID" if valid else "ERROR")
    return {
        "valid": valid,
        "errors": errors,
        "notes": notes,
        "results": results,
        "outcome": outcome,
    }


# ---------------------------------------------------------------------------
# Judge Execution & Accounting (D-S3-7, D-S3-9)
# ---------------------------------------------------------------------------

def _extract_worker_usage(worker_records: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, bool]]:
    """Extract usage counters from worker record with strict presence checking (A-P3-11)."""
    last = worker_records[-1] if worker_records else {}
    u_prov = last.get("usage", {})
    u_raw = last.get("usage_raw", {})
    u_pres = last.get("usage_presente", {})

    input_present = bool(u_pres.get("input", False))
    output_present = bool(u_pres.get("output", False))

    def _clean_counter(val: Any, name: str, present: bool) -> int | None:
        if val is not None:
            if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                raise JudgeValidationError(f"Invalid {name} in worker usage: expected nonnegative integer, got {val!r}")
        if not present or val is None:
            return None
        return val

    input_tokens = _clean_counter(u_prov.get("input_tokens"), "input_tokens", input_present)
    output_tokens = _clean_counter(u_prov.get("output_tokens"), "output_tokens", output_present)
    cache_tokens = _clean_counter(u_prov.get("cache_read_tokens"), "cache_read_tokens", bool(u_pres.get("cache_read", False)))
    thinking_tokens = _clean_counter(u_prov.get("thinking_tokens"), "thinking_tokens", bool(u_pres.get("thinking", False)))

    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "thinking_tokens": thinking_tokens,
    }
    presence = {
        "input": input_present and (input_tokens is not None),
        "output": output_present and (output_tokens is not None),
    }
    return usage, presence


def _run_single_attempt(
    attempt_dir: Path,
    prompt_file: Path,
    input_obj: dict[str, Any],
    expectations_obj: dict[str, Any],
    batch_id: str,
    attempt_id: str,
    worker_argv_prefix: list[str],
    call_timeout_s: float,
    wall_budget_s: float,
    t_start_wall: float,
    agy_shim: str,
    is_transport_retry: bool = False,
    is_canary_retry: bool = False,
) -> dict[str, Any]:
    """Execute a single attempt subprocess, preserve evidence, parse output, and validate."""
    worker_output_file = attempt_dir / "worker.json"
    cmd = list(worker_argv_prefix) + [
        "--encargo", str(prompt_file.resolve()),
        "--salida", str(worker_output_file.resolve()),
        "--modelo", DEFAULT_MODEL_ID,
        "--esfuerzo", DEFAULT_EFFORT,
        "--permiso", DEFAULT_PERMISSION,
        "--tope-wall", f"{call_timeout_s}s",
        "--parada", "-",
    ]

    env = os.environ.copy()
    env["OBRERO_VIA"] = DEFAULT_VIA
    env["OBRERO_AGY"] = agy_shim

    t_call_0 = time.monotonic()
    call_rc = 0
    call_error_msg: str | None = None
    stdout_bytes = b""
    stderr_bytes = b""

    # A-P3-9: Launch in dedicated process session/group, terminate and reap on timeout/failure
    rem_wall = max(1.0, wall_budget_s - (time.monotonic() - t_start_wall))
    actual_timeout = min(float(call_timeout_s), rem_wall)

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = proc.pid
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=actual_timeout)
        call_rc = proc.returncode
        if call_rc != 0:
            call_error_msg = f"Worker exited with rc={call_rc}: {stderr_bytes.decode('utf-8', errors='replace')[:200]}"
    except subprocess.TimeoutExpired:
        call_rc = 124
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            stdout_bytes, stderr_bytes = proc.communicate()
        call_error_msg = f"Worker call timed out after {call_timeout_s}s"
    except Exception as exc:
        call_rc = 1
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        stdout_bytes, stderr_bytes = proc.communicate()
        call_error_msg = f"Subprocess invocation failure: {exc}"
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    call_wall_ms = int(round((time.monotonic() - t_call_0) * 1000))

    # Keep worker.stdout.txt and worker.stderr.txt (L-12)
    with open(attempt_dir / "worker.stdout.txt", "wb") as f:
        f.write(stdout_bytes)
    with open(attempt_dir / "worker.stderr.txt", "wb") as f:
        f.write(stderr_bytes)

    # Copy any evidence files the worker wrote into attempt_dir (L-12)
    salida_parent = worker_output_file.parent
    evidence_names = [
        f"{worker_output_file.name}.rojo.json",
        f"{worker_output_file.stem}.rojo.json",
        f"{worker_output_file.name}.err",
        f"{worker_output_file.stem}.err",
    ]
    for name in evidence_names:
        cand = salida_parent / name
        if cand.is_file():
            dest = attempt_dir / name
            if cand.resolve() != dest.resolve():
                shutil.copy2(cand, dest)
    for vacio in list(salida_parent.glob(f"{worker_output_file.name}.vacio.*.txt")) + list(salida_parent.glob(f"{worker_output_file.stem}.vacio.*.txt")):
        if vacio.is_file():
            dest = attempt_dir / vacio.name
            if vacio.resolve() != dest.resolve():
                shutil.copy2(vacio, dest)

    if Path.cwd().resolve() != attempt_dir.resolve():
        for name in evidence_names:
            cand = Path.cwd() / name
            if cand.is_file():
                shutil.copy2(cand, attempt_dir / name)
        for vacio in list(Path.cwd().glob(f"{worker_output_file.name}.vacio.*.txt")) + list(Path.cwd().glob(f"{worker_output_file.stem}.vacio.*.txt")):
            if vacio.is_file():
                shutil.copy2(vacio, attempt_dir / vacio.name)

    if call_rc != 0 and (not stderr_bytes.strip()):
        for cand_name in ["worker.json.err", "worker.err", "worker.json.rojo.json", "worker.rojo.json"]:
            cand_path = attempt_dir / cand_name
            if cand_path.is_file():
                cand_content = cand_path.read_text(encoding="utf-8", errors="replace").strip()
                if cand_content:
                    call_error_msg = f"Worker exited with rc={call_rc}: {cand_content[:200]}"
                    break

    is_empty_or_missing_output = (
        not worker_output_file.is_file()
        or worker_output_file.stat().st_size == 0
        or worker_output_file.read_text(encoding="utf-8", errors="replace").strip() == ""
    )
    is_transport_failure = (call_rc != 0) or is_empty_or_missing_output

    worker_data: list[dict[str, Any]] = []
    parsed_response: dict[str, Any] = {}
    val_result: dict[str, Any] = {"valid": False, "errors": [], "notes": [], "results": []}
    usage_dict: dict[str, Any] = {}
    presence_dict: dict[str, bool] = {}

    # A-P3-11: Parse available usage receipts from output even on failed/aborted calls
    if worker_output_file.is_file() and worker_output_file.stat().st_size > 0:
        try:
            with open(worker_output_file, "r", encoding="utf-8") as wf:
                raw_w_data = json.load(wf)
            if isinstance(raw_w_data, list):
                worker_data = raw_w_data
                try:
                    usage_dict, presence_dict = _extract_worker_usage(worker_data)
                except Exception as exc:
                    val_result["errors"].append(str(exc))
        except Exception:
            pass

    if is_transport_failure:
        err_msg = call_error_msg or ("Worker output file missing or empty" if is_empty_or_missing_output else "Transport failure")
        val_result["errors"].append(err_msg)
    else:
        try:
            last_rec = worker_data[-1] if worker_data else {}
            if last_rec.get("is_error"):
                call_error_msg = f"Worker reported error: {last_rec.get('result')}"
                val_result["errors"].append(call_error_msg)
                is_transport_failure = True
            else:
                res_raw = last_rec.get("result", "")
                parsed_response = strict_json_loads(res_raw)
                val_result = validate_response(input_obj, parsed_response, expectations_obj)
                if val_result["valid"]:
                    # response.json feeds report construction; persist the accepted
                    # observation normalization. worker.json retains transport bytes.
                    parsed_response = dict(parsed_response)
                    parsed_response["results"] = [
                        {key: result[key] for key in ("check_id", "verdict", "confidence", "reason", "observations")}
                        for result in val_result["results"]]
                    contracts.validate_schema(parsed_response, "judge-output")
        except Exception as exc:
            val_result["errors"].append(f"Failed to process worker output: {exc}")

    # A-P3-7: Verify post-dispatch integrity of staged files and media
    input_file = attempt_dir / "input.json"
    if input_file.is_file():
        curr_in_sha = compute_file_sha256(input_file)
        exp_in_sha = expectations_obj.get("input_sha256")
        if exp_in_sha and curr_in_sha != exp_in_sha:
            val_result["valid"] = False
            val_result["errors"].append(f"input.json tampered after dispatch: {curr_in_sha} != {exp_in_sha}")

    if prompt_file.is_file():
        curr_pr_sha = compute_file_sha256(prompt_file)
        exp_pr_sha = expectations_obj.get("prompt_file_sha256") or expectations_obj.get("prompt_sha256")
        if exp_pr_sha and curr_pr_sha != exp_pr_sha:
            val_result["valid"] = False
            val_result["errors"].append(f"prompt.md tampered after dispatch: {curr_pr_sha} != {exp_pr_sha}")

    run_dir = attempt_dir.parent.parent
    for m in input_obj.get("media", []):
        m_p = Path(m["path"])
        m_full_path = m_p if m_p.is_absolute() else run_dir / m["path"]
        if not m_full_path.is_file():
            val_result["valid"] = False
            val_result["errors"].append(f"Media file {m['path']} missing after dispatch")
        else:
            curr_m_sha = compute_file_sha256(m_full_path)
            if curr_m_sha != m["sha256"]:
                val_result["valid"] = False
                val_result["errors"].append(
                    f"Media {m['evidence_id']} tampered after dispatch: {curr_m_sha} != {m['sha256']}"
                )

    # Write response.json and validation.json
    if parsed_response:
        with open(attempt_dir / "response.json", "w", encoding="utf-8") as rf:
            json.dump(parsed_response, rf, indent=2, sort_keys=True)

    validation_payload = {
        "valid": val_result["valid"],
        "errors": val_result["errors"],
        "notes": val_result.get("notes", []),
        "results": val_result["results"],
        "usage": usage_dict,
        "hashing_rule": "sha256_canonical_sorted_keys",
        "fixture_revision": "v1",
    }
    with open(attempt_dir / "validation.json", "w", encoding="utf-8") as vf:
        json.dump(validation_payload, vf, indent=2, sort_keys=True)

    attempt_record = {
        "attempt_id": attempt_id,
        "batch_id": batch_id,
        "rc": call_rc,
        "valid": val_result["valid"],
        "errors": val_result["errors"],
        "notes": val_result.get("notes", []),
        "wall_ms": call_wall_ms,
        "results": val_result["results"],
        "usage": usage_dict,
        "transport_retry": is_transport_retry,
        "canary_retry": is_canary_retry,
    }

    return {
        "is_transport_failure": is_transport_failure,
        "attempt_record": attempt_record,
        "val_result": val_result,
        "parsed_response": parsed_response,
        "usage_dict": usage_dict,
        "presence_dict": presence_dict,
        "call_rc": call_rc,
        "call_error_msg": call_error_msg,
        "call_wall_ms": call_wall_ms,
    }


# @anchor:bv.judge.run
def judge_run(
    capture: dict[str, Any],
    checklist: dict[str, Any],
    run_dir: Path,
    worker_argv_prefix: list[str] | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the multimodal judge stage across all batches and retries.

    Returns:
      {
        "schema": "vqa.judge-stage/v1",
        "attempts": list,
        "results": list,
        "accounting": dict,
        "errors": list,
      }
    """
    run_dir = Path(run_dir).resolve()
    t_start_wall = time.monotonic()

    # Validate policy if declared in checklist
    policy = checklist.get("judge_policy") or {}
    if policy:
        try:
            contracts.validate_schema(policy, "judge-policy")
        except Exception as exc:
            raise JudgeValidationError(f"Invalid judge policy: {exc}", code="invalid_judge_policy") from exc

    effective_budget = dict(policy)
    if budget:
        effective_budget.update(budget)

    max_calls = effective_budget.get("max_calls", MAX_CALLS)
    max_retries_per_batch = effective_budget.get("max_retries_per_batch", MAX_RETRIES_PER_BATCH)
    max_retry_calls = effective_budget.get("max_retry_calls", MAX_RETRY_CALLS)
    call_timeout_s = effective_budget.get("call_timeout_s", CALL_TIMEOUT_S)
    wall_budget_s = effective_budget.get("wall_budget_s", WALL_BUDGET_S)
    input_budget_tokens = effective_budget.get("input_budget_tokens", INPUT_BUDGET_TOKENS)
    input_per_call_tokens = effective_budget.get("input_per_call_tokens", INPUT_PER_CALL_TOKENS)
    transport_retry_backoff_s = float(
        effective_budget.get(
            "transport_retry_backoff_s",
            os.environ.get("BLENDER_VISUAL_QA_TRANSPORT_BACKOFF_S", DEFAULT_TRANSPORT_RETRY_BACKOFF_S),
        )
    )

    if worker_argv_prefix is None:
        if effective_budget.get("worker_argv_prefix"):
            worker_argv_prefix = list(effective_budget["worker_argv_prefix"])
        elif "BLENDER_VISUAL_QA_GEMINI_WORKER" in os.environ:
            worker_argv_prefix = [os.environ["BLENDER_VISUAL_QA_GEMINI_WORKER"]]
        elif "VQA_WORKER_ARGV_PREFIX" in os.environ:
            vqa_val = os.environ["VQA_WORKER_ARGV_PREFIX"]
            try:
                parsed = json.loads(vqa_val)
                worker_argv_prefix = parsed if isinstance(parsed, list) else [vqa_val]
            except Exception:
                worker_argv_prefix = [vqa_val]
        else:
            worker_argv_prefix = get_default_worker_argv_prefix()

    agy_shim = (
        effective_budget.get("agy_shim")
        or effective_budget.get("agy")
        or os.environ.get("BLENDER_VISUAL_QA_AGY")
        or get_default_agy_shim()
    )

    batches = build_batches(capture, checklist, run_dir)
    # Pre-dispatch verification for all media in batches before launching any worker (L-1)
    validate_media_pre_dispatch(
        [m for b in batches for m in b.get("input_obj", {}).get("media", [])],
        run_dir,
        min_duration_s=1.0,
    )
    if not batches:
        return {
            "schema": SCHEMA_JUDGE_STAGE,
            "attempts": [],
            "results": [],
            "accounting": {
                "worker_calls": 0,
                "gemini_calls": 0,
                "provider_attempts": None,
                "tokens": {"input_tokens": 0, "output_tokens": 0},
                "wall_ms": 0,
                "usd": None,
                "usd_status": "AUSENTE — no inventar",
            },
            "errors": [],
        }

    attempts_log: list[dict[str, Any]] = []
    final_results_by_check: dict[str, dict[str, Any]] = {}
    run_errors: list[str] = []

    # Initialize only checks with absent or incomplete capture points to CANNOT_SEE (A-P3-4)
    points_by_pid = {pt["point_id"]: pt for pt in capture.get("points", [])}
    frames_by_eid = {f["evidence_id"]: f for f in capture.get("frames", [])}
    clips_by_eid = {cl["evidence_id"]: cl for cl in capture.get("clips", [])}
    clips_by_pid = {cl["point_id"]: cl for cl in capture.get("clips", [])}

    for ch in checklist.get("checks", []):
        if ch.get("judge") is not None:
            cid = ch["id"]
            ch_media = str(ch.get("media", "")).lower()
            pids = ch.get("point_ids", [])
            has_all_declared_points = True
            for pid in pids:
                pt = points_by_pid.get(pid)
                if not pt:
                    has_all_declared_points = False
                    break
                if ch_media not in ("clip", "mp4", "video") and not (pt.get("selected_frame_ids") and any(sf in frames_by_eid for sf in pt["selected_frame_ids"])):
                    if ch_media in ("frame", "png"):
                        has_all_declared_points = False
                        break
                if ch_media not in ("frame", "png") and not (pt.get("clip_id") and (pt["clip_id"] in clips_by_eid or pt["clip_id"] in clips_by_pid)):
                    if ch_media in ("clip", "mp4", "video"):
                        has_all_declared_points = False
                        break
            if not has_all_declared_points:
                final_results_by_check[cid] = {
                    "check_id": cid,
                    "verdict": "CANNOT_SEE",
                    "confidence": 0.0,
                    "reason": "absent or incomplete capture points for check",
                    "observations": [],
                    "evidence_ids": [],
                }

    # Initialize every declared partition as unresolved, including undispatched ones.
    # A retry may replace only its own partition; capture gaps remain separate.
    partitions: dict[str, dict[str, dict[str, Any]]] = {}
    for cid, result in final_results_by_check.items():
        partitions[cid] = {"capture_gap": dict(result)}
    for batch in batches:
        for ch in batch.get("b_checks", batch.get("checks", [])):
            cid = ch["id"]
            partitions.setdefault(cid, {})[batch["batch_id"]] = {
                "check_id": cid, "verdict": "CANNOT_SEE", "confidence": 0.0,
                "reason": "Declared partition not evaluated", "observations": [],
                "evidence_ids": [],
            }

    def merge_partition(cid: str, batch_id: str, result: dict[str, Any]) -> None:
        previous = partitions[cid][batch_id]
        accepted = previous if previous["verdict"] == "FAIL" else result
        combined = dict(accepted)
        observations = list(previous.get("observations", []))
        for observation in result.get("observations", []):
            if observation not in observations:
                observations.append(observation)
        combined["observations"] = observations
        combined["evidence_ids"] = list(dict.fromkeys(
            previous.get("evidence_ids", []) + result.get("evidence_ids", [])))
        partitions[cid][batch_id] = combined

    worker_calls_count = 0
    retry_calls_count = 0
    cum_input_tokens = 0
    cum_output_tokens = 0
    call_inputs: list[int | None] = []
    call_outputs: list[int | None] = []
    submitted_media_count = 0
    submitted_media_duration = 0
    submitted_images = 0
    submitted_videos = 0
    submitted_render = 0
    submitted_attention = 0
    incomplete_partitions: list[dict[str, Any]] = []
    judge_dir = run_dir / "judge"

    def admission_reason(*, retry: bool, batch_retries: int, remaining_primary: int) -> str | None:
        if retry and (batch_retries >= max_retries_per_batch or retry_calls_count >= max_retry_calls):
            return "Retry calls budget exhausted"
        if worker_calls_count + (remaining_primary if retry else 0) >= max_calls:
            return "Worker calls budget exhausted; mandatory primary slots reserved"
        if cum_input_tokens >= input_budget_tokens:
            return "Cumulative input token budget exhausted"
        if retry and cum_input_tokens + input_per_call_tokens > input_budget_tokens:
            return "Insufficient input token budget for retry"
        if time.monotonic() - t_start_wall >= wall_budget_s:
            return "Judge wall budget exhausted"
        return None

    def dispatch(attempt: dict[str, Any], retry_cause: str | None) -> dict[str, Any]:
        nonlocal worker_calls_count, retry_calls_count, cum_input_tokens, cum_output_tokens
        nonlocal submitted_media_count, submitted_media_duration, submitted_images
        nonlocal submitted_videos, submitted_render, submitted_attention
        media = attempt["input_obj"]["media"]
        counts = {
            "count": len(media),
            "images": sum(m["kind"] == "png" for m in media),
            "video_entries": sum(m["kind"] == "mp4" for m in media),
            "duration_ms": sum(m.get("duration_ms", 0) for m in media if m["kind"] == "mp4"),
            "render": sum(m["role"] == "render" for m in media),
            "attention": sum(m["role"] == "attention" for m in media),
        }
        # Count only actual dispatches, including failures and retries, bound to
        # the exact serialized input rather than a canonicalized approximation.
        input_sha = compute_file_sha256(attempt["attempt_dir"] / "input.json")
        worker_calls_count += 1
        if retry_cause:
            retry_calls_count += 1
        submitted_media_count += counts["count"]
        submitted_media_duration += counts["duration_ms"]
        submitted_images += counts["images"]
        submitted_videos += counts["video_entries"]
        submitted_render += counts["render"]
        submitted_attention += counts["attention"]
        res = _run_single_attempt(
            attempt_dir=attempt["attempt_dir"], prompt_file=attempt["prompt_file"],
            input_obj=attempt["input_obj"], expectations_obj=attempt["expectations_obj"],
            batch_id=attempt["batch_id"], attempt_id=attempt["attempt_id"],
            worker_argv_prefix=worker_argv_prefix, call_timeout_s=call_timeout_s,
            wall_budget_s=wall_budget_s, t_start_wall=t_start_wall, agy_shim=agy_shim,
            is_transport_retry=retry_cause == "transport",
            is_canary_retry=retry_cause == "canary",
        )
        record = res["attempt_record"]
        record["retry_cause"] = retry_cause
        record["submitted_media"] = dict(counts, input_sha256=input_sha)
        attempts_log.append(record)
        presence, usage = res["presence_dict"], res["usage_dict"]
        call_in = usage.get("input_tokens") if presence.get("input") else None
        call_out = usage.get("output_tokens") if presence.get("output") else None
        call_inputs.append(call_in)
        call_outputs.append(call_out)
        if call_in is not None:
            cum_input_tokens += call_in
        if call_out is not None:
            cum_output_tokens += call_out
        return res

    for batch_index, batch in enumerate(batches):
        batch_id = batch["batch_id"]
        b_checks = batch.get("b_checks", batch.get("checks", []))
        remaining_primary = len(batches) - batch_index - 1
        reason = admission_reason(retry=False, batch_retries=0, remaining_primary=remaining_primary)
        if reason:
            for pending in batches[batch_index:]:
                incomplete_partitions.append({"batch_id": pending["batch_id"], "reason": reason})
            break
        attempt = batch
        retry_cause = None
        batch_retries = 0
        abstaining_checks: set[str] = set()
        while True:  # bounded by max_retries_per_batch and cumulative retry/call caps
            res = dispatch(attempt, retry_cause)
            call_in = call_inputs[-1]
            cap_reason = None
            if call_in is not None and call_in > input_per_call_tokens:
                cap_reason = f"Per-call input token budget exceeded ({call_in} > {input_per_call_tokens})"
            elif cum_input_tokens > input_budget_tokens:
                cap_reason = f"Cumulative input token budget exceeded ({cum_input_tokens} > {input_budget_tokens})"
            if cap_reason:
                # Budget incompleteness never replaces an already accepted FAIL.
                incomplete_partitions.append({"batch_id": batch_id, "reason": cap_reason})
                # An accepted verdict from this partition remains evidence. The
                # initialized CANNOT_SEE already represents checks without one.
                for ch in b_checks:
                    pending = partitions[ch["id"]][batch_id]
                    if pending["reason"] == "Declared partition not evaluated":
                        pending["reason"] = cap_reason
                break

            validation = res["val_result"]
            errors = validation["errors"]
            canary_only = bool(errors) and all(
                e.startswith("Attention ") and "canary failed" in e for e in errors)
            abstaining_checks = set()
            if validation["valid"]:
                for result in validation["results"]:
                    merge_partition(result["check_id"], batch_id, result)
                    if result["verdict"] == "CANNOT_SEE":
                        abstaining_checks.add(result["check_id"])
                cause = "abstention" if abstaining_checks else None
            elif res["is_transport_failure"]:
                cause = "transport"
            elif canary_only:
                cause = "canary"
            else:
                cause = None
                run_errors.extend(errors)
            if cause is None:
                break
            denied = admission_reason(retry=True, batch_retries=batch_retries,
                                      remaining_primary=remaining_primary)
            if denied:
                if not validation["valid"]:
                    run_errors.extend(errors)
                incomplete_partitions.append({"batch_id": batch_id, "reason": denied})
                break
            if cause == "transport" and transport_retry_backoff_s > 0:
                if time.monotonic() - t_start_wall + transport_retry_backoff_s >= wall_budget_s:
                    run_errors.extend(errors)
                    incomplete_partitions.append({"batch_id": batch_id, "reason": "Judge wall budget cannot admit transport backoff"})
                    break
                time.sleep(transport_retry_backoff_s)
            # Recheck before preparation and again before actual dispatch: preparation
            # itself may exhaust the judge wall allowance.
            denied = admission_reason(retry=True, batch_retries=batch_retries,
                                      remaining_primary=remaining_primary)
            if denied:
                incomplete_partitions.append({"batch_id": batch_id, "reason": denied})
                if not validation["valid"]:
                    run_errors.extend(errors)
                break
            next_attempt = _build_attempt_for_batch(
                capture=capture, checklist=checklist, run_dir=run_dir, batch_id=batch_id,
                b_checks=b_checks, attempt_id=f"{batch_id}_a{batch_retries + 2}",
                is_retry=cause == "abstention", abstaining_check_ids=abstaining_checks,
                allowed_source_ids=batch.get("allowed_source_ids"),
            )
            denied = admission_reason(retry=True, batch_retries=batch_retries,
                                      remaining_primary=remaining_primary)
            if denied:
                incomplete_partitions.append({"batch_id": batch_id, "reason": denied})
                if not validation["valid"]:
                    run_errors.extend(errors)
                break
            batch_retries += 1
            retry_cause = cause
            attempt = next_attempt

    # FAIL dominates; all declared partitions must PASS for a check-wide PASS.
    # Retain accepted evidence even when another partition is never dispatched.
    for cid, by_partition in partitions.items():
        results = list(by_partition.values())
        fails = [r for r in results if r["verdict"] == "FAIL"]
        unresolved = [r for r in results if r["verdict"] != "PASS"]
        selected = (fails or unresolved or results)[0]
        combined = dict(selected)
        combined["confidence"] = (selected["confidence"] if fails else
                                  0.0 if unresolved else min(r["confidence"] for r in results))
        missing = [r["reason"] for r in results if r["verdict"] == "CANNOT_SEE"]
        if fails and missing:
            combined["reason"] = f"{selected['reason']}; incomplete partition coverage: {'; '.join(missing)}"
        combined["observations"] = []
        combined["evidence_ids"] = []
        for result in results:
            for observation in result.get("observations", []):
                if observation not in combined["observations"]:
                    combined["observations"].append(observation)
            combined["evidence_ids"] = list(dict.fromkeys(
                combined["evidence_ids"] + result.get("evidence_ids", [])))
        final_results_by_check[cid] = combined
        for partition_id, result in by_partition.items():
            if result["verdict"] == "CANNOT_SEE":
                incomplete_partitions.append({"check_id": cid, "batch_id": partition_id,
                                              "reason": result["reason"]})

    # Build attempts.json
    total_wall_ms = int(round((time.monotonic() - t_start_wall) * 1000))
    input_complete = (worker_calls_count > 0 and len(call_inputs) == worker_calls_count and all(c is not None for c in call_inputs))
    output_complete = (worker_calls_count > 0 and len(call_outputs) == worker_calls_count and all(c is not None for c in call_outputs))

    complete_in = sum(c for c in call_inputs if c is not None) if input_complete else None
    complete_out = sum(c for c in call_outputs if c is not None) if output_complete else None
    partial_in = sum(c for c in call_inputs if c is not None)
    partial_out = sum(c for c in call_outputs if c is not None)

    accounting = {
        "worker_calls": worker_calls_count,
        "retry_calls": retry_calls_count,
        "gemini_calls": worker_calls_count,
        "provider_attempts": None,
        "tokens": {
            "input_tokens": complete_in,
            "output_tokens": complete_out,
            "partial_input_tokens": partial_in,
            "partial_output_tokens": partial_out,
            "complete": bool(input_complete and output_complete),
            "input_complete": input_complete,
            "output_complete": output_complete,
        },
        "submitted_media": {
            "count": submitted_media_count,
            "duration_ms": submitted_media_duration,
            "images": submitted_images,
            "video_entries": submitted_videos,
            "render": submitted_render,
            "attention": submitted_attention,
        },
        "incomplete_partitions": incomplete_partitions,
        "wall_ms": total_wall_ms,
        "usd": None,
        "usd_status": "AUSENTE — no inventar",
        "model": DEFAULT_MODEL_ID,
        "effort": DEFAULT_EFFORT,
    }

    attempts_payload = {
        "schema": "vqa.judge-attempts/v1",
        "attempts": attempts_log,
        "final_results": list(final_results_by_check.values()),
        "accounting": accounting,
        "incomplete_partitions": incomplete_partitions,
        "partition_results": partitions,
        "transport_retry": any(a.get("transport_retry", False) for a in attempts_log),
        "canary_retry": any(a.get("canary_retry", False) for a in attempts_log),
    }
    with open(judge_dir / "attempts.json", "w", encoding="utf-8") as af:
        json.dump(attempts_payload, af, indent=2, sort_keys=True)

    return {
        "schema": SCHEMA_JUDGE_STAGE,
        "attempts": attempts_log,
        "results": list(final_results_by_check.values()),
        "accounting": accounting,
        "errors": run_errors,
    }
