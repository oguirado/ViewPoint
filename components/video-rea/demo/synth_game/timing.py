# SPDX-License-Identifier: AGPL-3.0-only
"""Compare authored game timestamps with the encoded MP4 presentation timeline."""
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .media import MEDIA_TIMEOUT_S, run_media_tool


@dataclass(frozen=True)
class VideoTiming:
    pts_ms: tuple[Decimal, ...]
    duration_ms: Decimal


def probe_timing(video: Path) -> VideoTiming:
    data = json.loads(run_media_tool(
        "ffprobe",
        ["-v", "error", "-protocol_whitelist", "file", "-select_streams", "v:0",
         "-show_entries", "frame=pts_time:format=duration", "-of", "json", video.name],
        cwd=video.parent, timeout=MEDIA_TIMEOUT_S,
    ))
    return VideoTiming(
        tuple(Decimal(frame["pts_time"]) * 1000 for frame in data["frames"]),
        Decimal(data["format"]["duration"]) * 1000,
    )


def validate_timing(truth: dict, timing: VideoTiming, *, tolerance_ms: int = 1) -> None:
    if len(truth["frames"]) != len(timing.pts_ms):
        raise ValueError("MP4 frame count differs from ground truth")
    for frame, pts in zip(truth["frames"], timing.pts_ms, strict=True):
        if not pts.is_finite() or abs(pts - frame["pts_ms"]) > tolerance_ms:
            raise ValueError(f"PTS mismatch at frame {frame['frame_index']}")
    if truth["video"]["duration_ms"] != timing.duration_ms:
        raise ValueError("MP4 duration differs from ground truth")
