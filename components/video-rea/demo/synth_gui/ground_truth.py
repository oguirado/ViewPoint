# SPDX-License-Identifier: AGPL-3.0-only

"""Check the authored labels against their frame sequence and the MP4 timeline."""

import json
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from .media import run_media_tool
from .scene import EXITS


@dataclass(frozen=True)
class VideoTiming:
    pts_ms: tuple[Decimal, ...]
    duration_ms: Decimal


def probe_timing(video: Path) -> VideoTiming:
    output = run_media_tool(
        "ffprobe",
        [
            "-v", "error", "-protocol_whitelist", "file", "-select_streams", "v:0",
            "-show_entries", "frame=pts_time:format=duration", "-of", "json",
            video.name,
        ],
        cwd=video.parent,
    )
    data = json.loads(output)
    return VideoTiming(
        tuple(Decimal(frame["pts_time"]) * 1000 for frame in data["frames"]),
        Decimal(data["format"]["duration"]) * 1000,
    )


def validate_timing(truth: dict, timing: VideoTiming) -> None:
    frames = truth["frames"]
    if len(frames) != len(timing.pts_ms):
        raise ValueError("MP4 frame count differs from ground truth")
    for frame, pts in zip(frames, timing.pts_ms, strict=True):
        if not pts.is_finite() or abs(pts - frame["pts_ms"]) > 1:
            raise ValueError(f"PTS mismatch at frame {frame['frame_index']}")
    if truth["video"]["duration_ms"] != timing.duration_ms:
        raise ValueError("MP4 duration differs from ground truth")


def frame_witness(video_sha256: str, frame: dict) -> dict:
    return {
        "video_sha256": video_sha256, "frame_sha256": frame["frame_sha256"],
        "frame_index": frame["frame_index"], "pts_ms": frame["pts_ms"], "label": "observed",
    }


def transition_graph(frames: list[dict], *, note_min_length: int | None = None) -> dict:
    edges = {}
    for before, after in pairwise(frames):
        source, dest = before["screen_id"], after["screen_id"]
        if source == dest:
            continue
        target = after["action_target"]
        route = EXITS.get(source, {}).get(target)
        if after["frame_role"] != "action" or not route or route[0] != dest:
            raise ValueError("Screen change has no matching authored exit")
        field = route[1]
        guard = None
        if field:
            minimum = 1 if note_min_length is None else note_min_length
            if not any(w["id"] == field and len(w["text"]) >= minimum for w in before["widgets"]):
                raise ValueError("Screen change violates its guard")
            guard = {"field": field, "predicate": "nonempty"}
            if note_min_length is not None:
                guard.update(predicate="min_length", minimum=minimum)
        edge = edges.setdefault((source, target), {
            "from_screen": source, "to_screen": dest, "target": target,
            "guard": guard,
            "frame_indices": [], "action_ids": [],
        })
        edge["frame_indices"].append(after["frame_index"])
        edge["action_ids"].append(after["action_id"])
    screens = list(dict.fromkeys(frame["screen_id"] for frame in frames))
    return {
        "screens": screens, "edges": list(edges.values()),
        "alternatives": {
            screen: [
                {"target": control, "to_screen": dest, "observed": (screen, control) in edges}
                for control, (dest, _) in EXITS[screen].items()
            ] for screen in screens
        },
    }


def contains(outer, inner):
    ox, oy, ow, oh = outer
    x, y, w, h = inner
    return w > 0 and h > 0 and ox <= x <= ox+ow-w and oy <= y <= oy+oh-h


def validate_holdout_widgets(widgets, width, height, note_min_length):
    by_id = {widget["id"]: widget for widget in widgets}
    for widget in widgets:
        widget_id = widget["id"]
        slot = widget["slot_bbox"]
        ink = widget["text_bbox"]
        if not contains((0, 0, width, height), slot):
            raise ValueError(f"Widget slot outside frame: {widget_id}")
        if bool(widget["text"]) != (ink is not None) or (ink is not None and not contains(slot, ink)):
            raise ValueError(f"Invalid text bbox: {widget_id}")
        if widget["bbox"] != (ink if widget["kind"] == "text" else slot):
            raise ValueError(f"Widget bbox differs from painted geometry: {widget_id}")

        parent = widget["parent_id"]
        children = [row["id"] for row in widgets if row["parent_id"] == widget_id]
        if (parent is not None and (parent not in by_id or by_id[parent]["layer"] != widget["layer"])
                or widget["child_ids"] != children):
            raise ValueError(f"Inconsistent widget links: {widget_id}")

    if "save" in by_id:
        notes = by_id.get("notes")
        if notes is None or by_id["save"]["enabled"] != (len(notes["text"]) >= note_min_length):
            raise ValueError("Save button disagrees with its guard")


def validate_ground_truth(truth: dict) -> None:
    frames, actions, graph = truth["frames"], truth["actions"], truth["transition_graph"]
    width, height, duration = (
        truth["video"][field] for field in ("width", "height", "duration_ms")
    )
    holdout = truth.get("profile") == "holdout"
    note_min_length = truth.get("note_min_length") if holdout else None
    if holdout and (type(note_min_length) is not int or note_min_length < 1):
        raise ValueError("Invalid holdout note guard")
    if not frames or frames[0]["pts_ms"] != 0:
        raise ValueError("The first frame must start at PTS zero")
    if any(
        type(frame["pts_ms"]) is not int
        or frame["frame_index"] != index
        or not 0 <= frame["pts_ms"] < duration
        for index, frame in enumerate(frames)
    ) or any(before["pts_ms"] >= after["pts_ms"] for before, after in pairwise(frames)):
        raise ValueError("Invalid frame index or PTS order")

    for frame, end in zip(frames, [f["pts_ms"] for f in frames[1:]] + [duration], strict=True):
        if holdout:
            if frame["duration_ms"] != end - frame["pts_ms"]:
                raise ValueError("Frame duration differs from its PTS interval")
            validate_holdout_widgets(frame["widgets"], width, height, note_min_length)
        widget_ids = set()
        for widget in frame["widgets"]:
            x, y, w, h = widget["bbox"]
            if not (w > 0 and h > 0 and 0 <= x <= width - w and 0 <= y <= height - h):
                raise ValueError(f"Widget outside frame: {widget['id']}")
            if widget["id"] in widget_ids:
                raise ValueError(f"Duplicate widget: {widget['id']}")
            widget_ids.add(widget["id"])

    screens = set(graph["screens"])
    if any(
        edge["from_screen"] not in screens or edge["to_screen"] not in screens
        for edge in graph["edges"]
    ):
        raise ValueError("Transition references an unknown screen")
    if graph != transition_graph(frames, note_min_length=note_min_length):
        raise ValueError("Transition graph differs from the labelled frames")

    action_ids = []
    last_frame_index = 0
    for action in actions:
        pts, index = action["pts_ms"], action["frame_index"]
        if type(pts) is not int or not 0 <= pts < duration:
            raise ValueError(f"Action outside the video: {action['id']}")
        if not last_frame_index < index < len(frames) or pts != frames[index]["pts_ms"]:
            raise ValueError(f"Action PTS does not match its frame: {action['id']}")
        x, y = action["pointer"]
        targets = {widget["id"]: widget for widget in frames[index - 1]["widgets"]}
        target = targets.get(action["target"])
        if target is None or target["layer"] != "foreground" or not (0 <= x < width and 0 <= y < height):
            raise ValueError(f"Action has no visible target: {action['id']}")
        target_x, target_y, target_w, target_h = target["bbox"]
        if not (target_x <= x < target_x + target_w and target_y <= y < target_y + target_h):
            raise ValueError(f"Action misses its target: {action['id']}")
        frame = frames[index]
        if (frame["frame_role"] != "action" or frame["action_id"] != action["id"]
                or frame["action_target"] != action["target"] or frame["pointer"] != action["pointer"]):
            raise ValueError("Action witness differs from its result frame")
        if frame["screen_id"] != frames[index-1]["screen_id"] and action["kind"] != "click":
            raise ValueError("Screen change requires a click")
        if holdout and action["kind"] == "click" and action["target"] == "save":
            transitioned = frame["screen_id"] != frames[index-1]["screen_id"]
            if transitioned != target["enabled"]:
                raise ValueError("Save click disagrees with its guard")
        if action["provenance"] != frame_witness(truth["video"]["sha256"], frame):
            raise ValueError("Action provenance differs from its result frame")
        action_ids.append(action["id"])
        last_frame_index = index

    if [f["action_id"] for f in frames if f["frame_role"] == "action"] != action_ids:
        raise ValueError("Missing action for a labelled frame")

    workflow_actions = [action for flow in truth["workflows"] for action in flow["action_ids"]]
    if workflow_actions != action_ids or len(set(action_ids)) != len(action_ids):
        raise ValueError("Workflows must cover each action exactly once, in order")
