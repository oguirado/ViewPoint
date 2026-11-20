# SPDX-License-Identifier: AGPL-3.0-only
"""Local admission, sampling and atomic publication for both recorded inputs."""
from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile

from jsonschema import Draft202012Validator
from PIL import Image

from . import media, timeline
from .proc import ResourceLimitError
from .queue import FrameBlock, open_frame_source
from .records import COMPONENT, Contracts, canonical, digest


MAX_ARTIFACT_BYTES = 1 << 30


def milliseconds(base, ticks):
    ms = base * ticks * 1000
    return ms.numerator if ms.denominator == 1 else float(ms)


def sample(track, records, occurrences, period_ms, contracts):
    reasons = [list() for _ in records]
    reasons[0].append("endpoint")
    if len(records) > 1:
        reasons[-1].append("endpoint")
    anchor = None
    for i, tick in enumerate(track.pts):
        if anchor is None or (tick - anchor) * track.base * 1000 >= period_ms:
            reasons[i].append("periodic")
            anchor = tick
        if i and occurrences[i]["rgb_sha256"] != occurrences[i - 1]["rgb_sha256"]:
            reasons[i - 1].append("before-change")
            reasons[i].append("after-change")
    selected = [{"provenance": record["provenance"], "reason": ", ".join(why)}
                for record, why in zip(records, reasons, strict=True) if why]
    value = {"schema": "rea.keyframe_set/v1", "derived_from": [digest(canonical(records))],
             "evidence": [entry["provenance"] for entry in selected], "keyframes": selected}
    return contracts.checked("keyframe_set", value)


def validate_vision(manifest):
    engine = COMPONENT.parent / "vision-engine"
    sys.path.insert(0, str(engine))
    try:
        from visioncore.frame import validate_manifest
    finally:
        sys.path.remove(str(engine))
    schema = json.loads((engine / "visioncore/schemas/vqa.frame.v1.schema.json").read_bytes())
    Draft202012Validator(schema).validate(manifest)
    validate_manifest(manifest)


class Recording:
    def __init__(self, directory, track, contracts, queue, period, owner_gaps=()):
        self.directory, self.track, self.contracts = directory, track, contracts
        self.queue, self.period = queue, period
        self.occurrences, self.frames, self.vision = [], [], []
        self.seen = {}
        self.staged_bytes = 0
        self.gaps = list(owner_gaps)
        self.origin = min(0, track.pts[0])
        (directory / "artifacts").mkdir()

    def _write_artifact(self, name, data):
        total = self.staged_bytes + len(data)
        if total > MAX_ARTIFACT_BYTES:
            raise ResourceLimitError(f"FRAME_STORAGE_LIMIT: recording budget exceeded writing {name}")
        (self.directory / name).write_bytes(data)
        self.staged_bytes = total

    def accept(self, index, raw):
        track = self.track
        if index != len(self.frames) or len(raw) != track.width * track.height * 3:
            raise ValueError("incomplete or unordered frame")
        block = FrameBlock(raw, index, track.pts[index], track.width, track.height,
                           self.queue.epoch, index == len(track.pts) - 1)
        self.queue.offer(block.epoch, block, lambda: None)
        with self.queue.borrow(block.epoch) as read:
            self._store(read())

    def _store(self, block):
        track, i = self.track, block.index
        rgb_sha = digest(block.pixels)
        if rgb_sha in self.seen:
            first, artifact = self.seen[rgb_sha]
        else:
            buffer = BytesIO()
            with Image.frombytes("RGB", (track.width, track.height), block.pixels) as image:
                image.save(buffer, format="PNG")
            png = buffer.getvalue()
            artifact = digest(png)
            self._write_artifact(f"artifacts/{artifact}.png", png)
            self.seen[rgb_sha] = (i, artifact)
            first = None
        witness = {"video_sha256": track.source_sha, "frame_index": i,
                   "pts_ms": milliseconds(track.base, block.pts), "frame_sha256": artifact,
                   "label": "observed"}
        frame = {"schema": "rea.frame/v1", "frame_format": "png", "width": track.width,
                 "height": track.height, "provenance": witness}
        self.frames.append(self.contracts.checked("frame", frame))
        t_us = round((block.pts - self.origin) * track.base * 1000000)
        self.vision.append({"i": i, "t_us": t_us, "file": f"artifacts/{artifact}.png",
                            "sha256": rgb_sha, "file_sha256": artifact})
        self.occurrences.append({"frame_index": i, "pts": block.pts, "rgb_sha256": rgb_sha,
                                 "artifact_ref": f"af://sha256/{artifact}", "duplicate_of": first})
        if i:
            dt = block.pts - track.pts[i - 1]
            if dt == 0 or dt * track.base * 1000 > self.period:
                self.gaps.append({"kind": "repeated_pts" if dt == 0 else "sampling_gap",
                                  "before_index": i - 1, "after_index": i, "delta_ticks": dt})

    def finish(self):
        t = self.track
        if len(self.frames) != len(t.pts):
            raise ValueError("incomplete frame join")
        base = {"numerator": t.base.numerator, "denominator": t.base.denominator}
        intervals = [b - a for a, b in zip(t.pts, t.pts[1:])]
        distinct = set(intervals)
        # Repeated ticks signal a timestamp anomaly but supply no cadence evidence.
        if len(distinct - {0}) > 1:
            vfr = True
        elif len(intervals) >= 2 and 0 not in distinct:
            vfr = False
        else:
            vfr = {"value": None, "reason": "insufficient distinct presentation times for cadence"}
        duration = t.duration_ms
        if duration is None:
            duration = {"value": None, "reason": "source does not declare a duration"}
        clock_source = t.clock
        if t.clock == "captured_mono_ns":
            clock_source = {"value": None, "reason": "owner segment uses captured_mono_ns; no container PTS"}
        source = {"schema": "rea.video_source/v1",
                  "provenance": {"video_sha256": t.source_sha, "byte_size": t.byte_size},
                  "source_kind": t.kind, "media_type": t.media_type, "time_base": base,
                  "stream_index": t.index, "start_pts": t.pts[0], "duration_ms": duration,
                  "frame_count": len(t.pts), "width": t.width, "height": t.height,
                  "codec": t.codec, "container": t.container, "vfr": vfr, "pts_source": clock_source}
        self.contracts.checked("video_source", source)
        clock = {"video_sha256": t.source_sha, "byte_size": t.byte_size, "time_base": base,
                 "stream_index": t.index, "pts_source": t.clock, "packet_clock": t.packet_clock,
                 "start_pts": t.pts[0],
                 "duration_ms": t.duration_ms, "frame_count": len(t.pts),
                 "width": t.width, "height": t.height, "codec": t.codec, "container": t.container,
                 "decoder": t.decoder, "artifact_bytes": self.staged_bytes,
                 "irregular_pts": 0 in distinct or len(distinct) > 1,
                 "sample_period_ms": self.period, "vision_origin_pts": self.origin,
                 "geometry_epoch": 0, "crop_xywh": [0, 0, t.width, t.height],
                 "toro_mode": self.queue.mode, "fallback_reason": self.queue.fallback_reason,
                 "occurrences": self.occurrences, "gaps": self.gaps}
        manifest = {"schema": "vqa.frame/v1", "storage": "png_dir", "count": len(t.pts),
                    "channels": 3, "pixel_format": "rgb24", "width": t.width, "height": t.height,
                    "scope": {"app": "video_rea", "scene": "recorded"}, "frames": self.vision,
                    "provenance": {"video_sha256": t.source_sha, "time_base": base,
                                   "origin_pts": self.origin}}
        validate_vision(manifest)
        keyframes = sample(t, self.frames, self.occurrences, self.period, self.contracts)
        refs = {}
        for name, value in [("video_source.json", source), ("frames.json", self.frames),
                            ("frames.manifest.json", manifest), ("clock.json", clock),
                            ("keyframes.json", keyframes)]:
            data = canonical(value)
            self._write_artifact(name, data)
            refs[name] = "af://sha256/" + digest(data)
        self._write_artifact("refs.json", canonical(refs))


def compare_resume(stage, destination):
    # Replay the original source. Checking only hashes supplied inside the old
    # manifest would let a coherent but foreign set of witnesses resume.
    expected_paths = {p.relative_to(stage) for p in stage.rglob("*")}
    for path in destination.rglob("*"):
        if path.is_symlink() or path.relative_to(destination) not in expected_paths:
            raise ValueError("resume contains unexpected artifact")
    for relative in sorted(expected_paths):
        expected = stage / relative
        actual = destination / expected.relative_to(stage)
        if actual.is_symlink() or (expected.is_dir() and not actual.is_dir()):
            raise ValueError("resume artifact directory mismatch")
        if expected.is_file():
            size = expected.stat().st_size
            try:
                same = timeline.read_regular(actual, size) == expected.read_bytes()
            except (ValueError, OSError):
                same = False
            if not same:
                raise ValueError(f"resume witness mismatch: {expected.name}")


def _publish(out, period, resume, prepare_track, mode):
    if type(period) is not int or period <= 0:
        raise ValueError("sampling period must be positive milliseconds")
    out = Path(out).absolute()
    if out.is_symlink() or (resume and not out.is_dir()):
        raise ValueError("invalid resume or output directory")
    if out.exists() and not resume and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("output directory occupied")
    contracts = Contracts()
    queue = open_frame_source(mode)
    with tempfile.TemporaryDirectory(prefix=".rea-", dir=out.parent) as private:
        stage = Path(private) / "recording"
        stage.mkdir()
        try:
            track, decode, gaps = prepare_track(Path(private))
            if resume:
                try:
                    previous = json.loads(timeline.read_regular(out / "video_source.json", 65536))
                    identity = previous["provenance"]
                except (OSError, KeyError, TypeError, ValueError) as exc:
                    raise ValueError("resume source provenance unavailable") from exc
                if identity != {"video_sha256": track.source_sha, "byte_size": track.byte_size}:
                    raise ValueError("resume source artifact mismatch")
            record = Recording(stage, track, contracts, queue, period, gaps)
            decode(record.accept)
            record.finish()
        finally:
            queue.expire()
        if resume:
            compare_resume(stage, out)
        else:
            os.rename(stage, out)
    return out


def ingest(path, out, *, sample_ms=200, resume=False, mode=None):
    def prepare(private):
        sealed = private / "container"
        source_sha, size = media.snapshot(path, sealed)
        try:
            track = media.probe(sealed, source_sha, size)
        except ResourceLimitError:
            raise
        except (ValueError, KeyError) as exc:
            raise ValueError(f"container decode probe failed: {exc}") from exc
        return track, lambda accept: media.decode(sealed, track, accept), []
    return _publish(out, sample_ms, resume, prepare, mode)


def _ingest_sequence(data, expected_sha, borrow, out, period, resume, mode, count=None):
    def prepare(private):
        track, frames, gaps = timeline.prepare(data, expected_sha)
        if count is not None and len(frames) != count:
            raise ValueError("incomplete timeline/file join")
        return track, lambda accept: timeline.decode_sequence(track, frames, borrow, accept), gaps
    return _publish(out, period, resume, prepare, mode)


def ingest_timeline(data, out, *, expected_sha256, sequence, owner, first_index=0,
                    sample_ms=200, resume=False, mode=None):
    """Import exact sealed timeline bytes using an already admitted FileFrameSequence."""
    borrow = timeline.owner_borrow(sequence, owner, first_index)
    return _ingest_sequence(data, expected_sha256, borrow, out, sample_ms, resume, mode)


def ingest_timeline_files(path, out, *, expected_sha256, files, sample_ms=200,
                          resume=False, mode=None):
    """CLI counterpart for an owner-supplied ordered export of PNG slots."""
    @contextmanager
    def borrow(i):
        yield timeline.read_regular(files[i], timeline.MAX_OWNER_PNG_BYTES, limit_code="FRAME_BYTES_LIMIT")

    data = timeline.read_regular(path, timeline.MAX_TIMELINE_BYTES)
    return _ingest_sequence(data, expected_sha256, borrow, out, sample_ms, resume, mode, len(files))
