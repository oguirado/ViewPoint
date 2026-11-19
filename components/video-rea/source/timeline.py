# SPDX-License-Identifier: AGPL-3.0-only
"""Import a sealed owner snapshot and its explicitly supplied, ordered PNG sequence."""
from contextlib import contextmanager
from fractions import Fraction
from io import BytesIO
import json
import os
import stat

from jsonschema import Draft202012Validator, ValidationError
from PIL import Image
from referencing import Registry, Resource

from .media import MAX_FRAME, Track, check_extent
from .proc import ResourceLimitError
from .records import COMPONENT, digest


MAX_TIMELINE_BYTES = 8 << 20
MAX_OWNER_PNG_BYTES = MAX_FRAME + 65536


def read_regular(path, limit, *, limit_code="INPUT_BYTES_LIMIT"):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise ValueError("invalid sealed artifact extent")
        if info.st_size > limit:
            raise ResourceLimitError(f"{limit_code}: sealed artifact exceeds admission limit")
        data = stream.read(info.st_size + 1)
        if len(data) != info.st_size:
            raise ValueError("sealed artifact changed")
        return data


def prepare(data, expected_sha):
    if len(data) > MAX_TIMELINE_BYTES:
        raise ResourceLimitError("INPUT_BYTES_LIMIT: timeline exceeds admission limit")
    if digest(data) != expected_sha:
        raise ValueError("timeline does not match owner-attested digest")
    schemas = COMPONENT.parent / "vp-direct/schemas"
    frame = json.loads((schemas / "vp.frame_v1.schema.json").read_bytes())
    timeline = json.loads((schemas / "rec.timeline_v1.schema.json").read_bytes())
    registry = Registry().with_resource(frame["$id"], Resource.from_contents(frame))
    value = json.loads(data)
    try:
        Draft202012Validator(timeline, registry=registry).validate(value)
    except ValidationError:
        raise ValueError("invalid rec.timeline/v1") from None
    previous, identity = -1, None
    frames, gaps, holds = [], [], {}
    intervals = []
    for event in value["events"]:
        t = event["mono_ns"]
        binding = (event["capsule_id"], event["incarnation"])
        if t < previous or (identity is not None and identity != binding):
            raise ValueError("timeline order or capsule incarnation changed")
        previous, identity = t, binding
        if event.get("gap"):
            op = event.get("op", "")
            reason, _, edge = op.rpartition("_gap_")
            if reason not in {"privacy", "sleep", "human"} or "frame" in event:
                raise ValueError("unresolved timeline gap")
            if edge == "start" and reason not in holds:
                holds[reason] = t
            elif edge == "end" and reason in holds:
                intervals.append((holds.pop(reason), t))
            else:
                raise ValueError("unpaired timeline gap")
            gaps.append({"kind": "owner_gap", "pts": t, "event": op})
        elif "frame" in event:
            f = event["frame"]
            if holds or f["frame_kind"] != "cdp_png":
                raise ValueError("frame under hold or unsupported owner representation")
            frames.append((t, f))
    if holds:
        raise ValueError("unresolved owner hold at segment seal")
    if not frames:
        raise ValueError("missing owner frame sequence")
    w, h = frames[0][1]["width"], frames[0][1]["height"]
    check_extent(w, h, len(frames))
    pts = []
    # Merge overlapping holds once. Both the event and capture clocks must be clear.
    merged = []
    for start, end in sorted(intervals, key=lambda span: span[0]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    def check_clock(stamps):
        span = 0
        for stamp in stamps:
            while span < len(merged) and merged[span][1] <= stamp:
                span += 1
            if span < len(merged) and merged[span][0] <= stamp:
                raise ValueError("frame intersects owner hold")
    for _, f in frames:
        t = f["captured_mono_ns"]
        if (w, h) != (f["width"], f["height"]) or (pts and t < pts[-1]):
            raise ValueError("owner geometry or capture order changed")
        pts.append(t)
    check_clock(pts)
    check_clock(t for t, _ in frames)
    track = Track("file", "application/json", 0, Fraction(1, 10**9), w, h, pts,
                  "png", "rec.timeline/v1", "captured_mono_ns", None,
                  "Pillow PNG", expected_sha, len(data), "owner_capture")
    return track, [f for _, f in frames], gaps


def decode_sequence(track, descriptors, borrow, accept):
    """borrow(index) yields bytes from an admitted file or FileFrameSequence slot."""
    for i, f in enumerate(descriptors):
        with borrow(i) as payload:
            if len(payload) > MAX_OWNER_PNG_BYTES:
                raise ResourceLimitError("FRAME_BYTES_LIMIT: owner PNG exceeds admission limit")
            if digest(payload) != f["frame_sha256"]:
                raise ValueError("owner frame hash mismatch")
            with Image.open(BytesIO(payload), formats=["PNG"]) as image:
                if image.size != (track.width, track.height):
                    raise ValueError("owner frame geometry mismatch")
                accept(i, image.convert("RGB").tobytes())


def owner_borrow(sequence, owner, first_index):
    @contextmanager
    def borrow(i):
        with sequence.borrow(owner, first_index + i) as lease:
            if lease.frame.index != first_index + i:
                raise ValueError("owner slot identity mismatch")
            if lease.frame.size_bytes > MAX_OWNER_PNG_BYTES:
                raise ResourceLimitError("FRAME_BYTES_LIMIT: owner slot exceeds admission limit")
            yield lease.read()
    return borrow
