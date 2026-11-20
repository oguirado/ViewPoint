# SPDX-License-Identifier: AGPL-3.0-only
"""Admit the content-addressed W1 manifest, sidecars and decoded PNGs."""
from fractions import Fraction
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat

import numpy as np
from PIL import Image

from source.ingest import validate_vision
from source.records import digest

from .temporal import luma


MANIFEST = "frames.manifest.json"
SIDECARS = (MANIFEST, "frames.json", "clock.json", "keyframes.json")
HEX = re.compile(r"[0-9a-f]{64}")
MAX_BYTES = 32 << 20


def locator(sha):
    return f"af://sha256/{sha}"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(reader, name):
    data = reader(name)
    require(isinstance(data, bytes) and 0 < len(data) <= MAX_BYTES, "invalid admitted input bytes")
    return data


def pinned_records(reader):
    refs = json.loads(read(reader, "refs.json"))
    records, parents = [], []
    for name in SIDECARS:
        data = read(reader, name)
        sha = digest(data)
        require(refs.get(name) == locator(sha), f"W1 sidecar pin mismatch: {name}")
        records.append(json.loads(data))
        parents.append(sha)
    return records, parents


class DirectoryInputs:
    """Local CLI adapter. Provider callers supply their own admitted reader."""
    def __init__(self, manifest):
        path = Path(manifest)
        require(path.name == MANIFEST, f"expected {MANIFEST}")
        self.root = path.parent

    def read(self, name):
        require(name in (*SIDECARS, "refs.json") or
                re.fullmatch(r"artifacts/[0-9a-f]{64}\.png", name), "unrecognized W1 input name")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        folder = os.open(self.root, flags)
        try:
            if name.startswith("artifacts/"):
                child = os.open("artifacts", flags, dir_fd=folder)
                os.close(folder)
                folder = child
            fd = os.open(name.rsplit("/", 1)[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=folder)
            try:
                require(stat.S_ISREG(os.fstat(fd).st_mode), "W1 input is not a regular file")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    return stream.read(MAX_BYTES + 1)
            finally:
                os.close(fd)
        finally:
            os.close(folder)


class Recording:
    def __init__(self, reader):
        self.read = reader
        (self.manifest, self.frames, self.clock, keyframes), self.parents = pinned_records(reader)
        m, c = self.manifest, self.clock
        require(m.get("storage") == "png_dir", "expected W1 png_dir storage")
        validate_vision(m)
        self.source = c["video_sha256"]
        self.width, self.height = m["width"], m["height"]
        require(isinstance(self.source, str) and HEX.fullmatch(self.source)
                and (self.width, self.height) == (c["width"], c["height"]),
                "inconsistent W1 recording identity")
        self.time_base = c["time_base"]
        require(set(self.time_base) == {"numerator", "denominator"}
                and all(type(v) is int and v > 0 for v in self.time_base.values()),
                "invalid rational time base")
        self.base = Fraction(self.time_base["numerator"], self.time_base["denominator"])
        self.origin = c["vision_origin_pts"]
        require(type(self.origin) is int and m["provenance"] == {
            "video_sha256": self.source, "time_base": self.time_base, "origin_pts": self.origin
        }, "inconsistent W1 presentation clock")
        self.occurrences = c["occurrences"]
        require(m["count"] == c["frame_count"] == len(self.frames) == len(self.occurrences),
                "incomplete W1 frame join")
        require(keyframes["schema"] == "rea.keyframe_set/v1"
                and keyframes["derived_from"] == [self.parents[1]],
                "keyframe lineage mismatch")
        self.key_indices = set()
        require(keyframes["evidence"] == [entry["provenance"] for entry in keyframes["keyframes"]],
                "keyframe evidence mismatch")
        for witness in keyframes["evidence"]:
            index = witness["frame_index"]
            require(type(index) is int and 0 <= index < len(self.frames)
                    and self.frames[index]["provenance"] == witness and index not in self.key_indices,
                    "keyframe occurrence mismatch")
            self.key_indices.add(index)
        self.gaps = c["gaps"]

    def __iter__(self):
        last, seen = self.origin, {}
        entries = zip(self.manifest["frames"], self.frames, self.occurrences, strict=True)
        for index, (entry, record, occurrence) in enumerate(entries):
            png_sha, rgb_sha = entry["file_sha256"], entry["sha256"]
            tick = occurrence["pts"]
            require(type(tick) is int and tick >= last and entry["i"] == index
                    and occurrence["frame_index"] == index and occurrence["rgb_sha256"] == rgb_sha
                    and occurrence["artifact_ref"] == locator(png_sha)
                    and entry["t_us"] == round((tick - self.origin) * self.base * 1_000_000),
                    f"W1 occurrence or PTS mismatch at frame {index}")
            if index == 0:
                require(tick == self.clock["start_pts"], "W1 starting PTS mismatch")
            last = tick
            expected = seen.get(rgb_sha)
            require(occurrence["duplicate_of"] == (expected[0] if expected else None)
                    and (expected is None or expected[1] == png_sha), "W1 duplicate lineage mismatch")
            seen.setdefault(rgb_sha, (index, png_sha))
            moment = tick * self.base
            pts_ms = moment * 1000
            provenance = {
                "frame_index": index, "video_sha256": self.source,
                "pts_ms": int(pts_ms) if pts_ms.denominator == 1 else float(pts_ms),
                "frame_sha256": png_sha, "label": "observed",
            }
            require(record["schema"] == "rea.frame/v1" and record["frame_format"] == "png"
                    and record["provenance"] == provenance
                    and (record["width"], record["height"]) == (self.width, self.height),
                    f"W1 record mismatch at frame {index}")
            path = f"artifacts/{png_sha}.png"
            require(entry["file"] == path, "invalid W1 PNG locator")
            raw = read(self.read, path)
            require(digest(raw) == png_sha, f"PNG file pin mismatch at frame {index}")
            with Image.open(BytesIO(raw), formats=["PNG"]) as png:
                require(png.mode == "RGB" and png.size == (self.width, self.height),
                        "W1 PNG format or extent mismatch")
                rgb = np.array(png)
            require(digest(rgb.tobytes()) == rgb_sha, f"raw RGB pin mismatch at frame {index}")
            descriptor = b"segment/luma8/v1\x00" + self.width.to_bytes(4, "big") + self.height.to_bytes(4, "big")
            yield rgb, {
                "provenance": provenance, "occurrence": index, "pts_ticks": tick,
                "pts": {"numerator": moment.numerator, "denominator": moment.denominator},
                "artifact_ref": locator(png_sha), "png_sha256": png_sha, "rgb_sha256": rgb_sha,
                "normalized_sha256": digest(descriptor + luma(rgb).tobytes()),
                "duplicate_of": occurrence["duplicate_of"], "keyframe": index in self.key_indices,
            }
