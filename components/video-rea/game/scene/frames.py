# SPDX-License-Identifier: AGPL-3.0-only
"""Read only admitted W1 PNG occurrences and their presentation clock."""
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
import json
from pathlib import Path
import re

import numpy as np
from PIL import Image

from source.records import digest


def vision_origin(first_ticks):
    """Return W1's origin for the manifest clock."""
    return min(0, first_ticks)


def vision_milliseconds(ticks, time_base, origin):
    ms = (ticks - origin) * time_base * 1000
    return ms.numerator if ms.denominator == 1 else float(ms)


@dataclass(frozen=True)
class Frame:
    image: np.ndarray
    index: int
    ticks: int
    time_base: Fraction
    video_sha256: str
    file_sha256: str
    rgb_sha256: str
    vision_origin_pts: int = 0

    @property
    def provenance(self):
        return {"video_sha256": self.video_sha256, "frame_index": self.index,
                "pts_ms": vision_milliseconds(self.ticks, self.time_base, self.vision_origin_pts),
                "frame_sha256": self.file_sha256, "label": "observed"}


def read_frames(directory):
    """Join W1 occurrences in presentation order, retaining repeated timestamps."""
    root = Path(directory).resolve(strict=True)
    try:
        yield from _read_frames(root)
    except (KeyError, TypeError, ZeroDivisionError) as exc:
        raise ValueError("invalid W1 frame source metadata") from exc


def _read(root, relative):
    path = root / relative
    if path.resolve(strict=True) != path:
        raise ValueError("W1 artifact paths must not traverse symlinks")
    return path.read_bytes()


def _read_frames(root):
    clock = json.loads(_read(root, "clock.json"))
    manifest = json.loads(_read(root, "frames.manifest.json"))
    if (manifest["schema"], manifest["storage"], manifest["pixel_format"], manifest["channels"]) != (
        "vqa.frame/v1", "png_dir", "rgb24", 3
    ):
        raise ValueError("expected a W1 RGB PNG directory")
    video = clock["video_sha256"]
    base = clock["time_base"]
    origin = clock["vision_origin_pts"]
    dimensions = manifest["width"], manifest["height"]
    rows, occurrences = manifest["frames"], clock["occurrences"]
    count = len(rows)
    if (not count or count != len(occurrences) or count != clock["frame_count"]
            or count != manifest["count"] or dimensions != (clock["width"], clock["height"])
            or manifest["provenance"] != {"video_sha256": video, "time_base": base, "origin_pts": origin}
            or not re.fullmatch(r"[0-9a-f]{64}", video)
            or any(type(base[key]) is not int or base[key] <= 0 for key in ("numerator", "denominator"))
            or type(origin) is not int or origin != vision_origin(occurrences[0]["pts"])):
        raise ValueError("W1 clock and manifest disagree")
    time_base = Fraction(base["numerator"], base["denominator"])
    previous = origin
    for index, (row, occurrence) in enumerate(zip(rows, occurrences, strict=True)):
        file_hash, rgb_hash, ticks = row["file_sha256"], row["sha256"], occurrence["pts"]
        if not re.fullmatch(r"[0-9a-f]{64}", file_hash):
            raise ValueError(f"invalid W1 PNG digest at occurrence {index}")
        filename = f"artifacts/{file_hash}.png"
        if (row["i"] != index or occurrence["frame_index"] != index or type(ticks) is not int
                or ticks < previous or row["t_us"] != round((ticks - origin) * time_base * 1000000)
                or row["file"] != filename or occurrence["artifact_ref"] != f"af://sha256/{file_hash}"
                or occurrence["rgb_sha256"] != rgb_hash):
            raise ValueError(f"unjoined W1 occurrence {index}")
        data = _read(root, filename)
        if digest(data) != file_hash:
            raise ValueError(f"W1 PNG digest mismatch at occurrence {index}")
        with Image.open(BytesIO(data), formats=["PNG"]) as png:
            if png.size != dimensions or png.mode != "RGB" or digest(png.tobytes()) != rgb_hash:
                raise ValueError(f"W1 decoded raster mismatch at occurrence {index}")
            image = np.asarray(png)
        yield Frame(image, index, ticks, time_base, video, file_hash, rgb_hash, origin)
        previous = ticks
