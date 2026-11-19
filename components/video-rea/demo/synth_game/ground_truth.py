# SPDX-License-Identifier: AGPL-3.0-only
"""Verify a published holdout from its input tape, frame images and MP4."""
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw

from .holdout import HUD_WIDTH, PALETTE_ROLES
from .render import FONT, PANELS
from .replay import check_trajectory
from .timing import probe_timing, validate_timing


ACTOR_EXTENTS = {
    "player": (-4, -12, 5, 0),
    "walker": (-4, -4, 5, 5),
    "firefly": (-6, -4, 7, 5),
    "pickup": (-3, -4, 4, 5),
}


def match(label, actual, expected):
    if actual != expected:
        raise ValueError(f"Holdout truth mismatch: {label}")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def box_label(kind, box, width, height, **extra):
    projected = list(box) if box is not None else None
    visible = None
    if box is not None:
        left, top = max(0, box[0]), max(0, box[1])
        right, bottom = min(width, box[2]), min(height, box[3])
        if left < right and top < bottom:
            visible = [left, top, right, bottom]
    return {"kind": kind, "bbox_px": projected, "viewport_bbox_px": visible, **extra}


def check_visuals(frame, image, truth, colors):
    width, height = image.size
    camera = frame["camera_x_px"]
    floor = truth["rules"]["floor_y_px"]
    goal = truth["rules"]["goal_x_px"] - camera
    actors = {"beacon": box_label("goal", (goal, floor - 24, goal + 12, floor), width, height)}
    for name, pose in frame["entities"].items():
        kind = "pickup" if name in truth["pickup_centres_px"] else name
        x, y = round(pose["x_px"] - camera), round(pose["y_px"])
        left, top, right, bottom = ACTOR_EXTENTS[kind]
        box = None if pose["state"] == "collected" else (x + left, y + top, x + right, y + bottom)
        actors[name] = box_label(kind, box, width, height)
    match("actor geometry", frame["actor_boxes"], actors)

    x, y = truth["hud_origin_px"]
    hud_box = (x, y, x + HUD_WIDTH, y + 16)
    widgets = {"hud": box_label("panel", hud_box, width, height)}
    ink = ImageDraw.Draw(image)
    text = [
        ("score", (x + 4, y + 1), f"SCORE  {frame['score']:03d}", None),
        ("lives", (x + HUD_WIDTH - 55, y + 1), f"LIVES  {frame['lives']}", None),
    ]
    if frame["screen"] in PANELS:
        title, subtitle = PANELS[frame["screen"]]
        widgets["overlay"] = box_label("panel", (31, 34, 161, 79), width, height)
        text += [("overlay_title", (width // 2, 47), title, "mm"),
                 ("overlay_subtitle", (width // 2, 65), subtitle, "mm")]
    for name, position, value, anchor in text:
        widget_box = ink.textbbox(position, value, font=FONT, anchor=anchor)
        widgets[name] = box_label("text", widget_box, width, height, text=value)
    match("widget geometry and text", frame["widgets"], widgets)
    match("HUD raster position", [image.getpixel(point) for point in ((x, y), (hud_box[2] - 1, y + 15))],
          [colors["hud"]] * 2)

    level = truth["level"]
    row = level["rows"][floor // level["tile_px"]]
    # A bottom HUD is drawn over the floor. Test the unoccluded strip of that scan line.
    sample_x = [px for px in range(width) if hud_box[3] < height or not x <= px < hud_box[2]]
    actual = [image.getpixel((px, height - 1)) for px in sample_x]
    expected = [colors["floor" if row[(camera + px) // level["tile_px"]] == "#" else "sky"]
                for px in sample_x]
    match("terrain raster and palette", actual, expected)


def check_recording(directory):
    """Raise ValueError on an inconsistent holdout, or return the maximum replay error in pixels."""
    directory = Path(directory)
    truth = json.loads((directory / "ground_truth.json").read_text(encoding="utf-8"))
    match("profile", truth["profile"], "holdout")
    frames, video, level = truth["frames"], truth["video"], truth["level"]
    match("artifact paths", (truth["inputs"], video["path"]), ("inputs.jsonl", "recording.mp4"))
    inputs = [json.loads(line) for line in (directory / "inputs.jsonl").read_text(encoding="utf-8").splitlines()]
    movie = directory / "recording.mp4"
    match("recording hash", sha256(movie), video["sha256"])
    timing = probe_timing(movie)
    validate_timing(truth, timing, tolerance_ms=0)
    gaps = {after - before for before, after in zip(timing.pts_ms, timing.pts_ms[1:])}
    match("variable presentation gaps", len(gaps) > 1, True)
    width, height = video["width"], video["height"]
    match("level width", {len(row) * level["tile_px"] for row in level["rows"]}, {level["width_px"]})
    match("level covers viewport", len(level["rows"]) * level["tile_px"] >= height, True)
    match("level tiles", set("".join(level["rows"])), {".", "#"})
    match("palette roles", set(truth["palette"]), set(PALETTE_ROLES))
    colors = {name: ImageColor.getrgb(color) for name, color in truth["palette"].items()}

    previous_tick = -1
    provenance = []
    transitions = []
    previous_screen = "title"
    for index, (frame, row) in enumerate(zip(frames, inputs, strict=True)):
        pts = frame["tick"] * truth["physics"]["fixed_step_ms"]
        match("frame/input clock", (frame["frame_index"], row["frame_index"], frame["pts_ms"], row["pts_ms"]),
              (index, index, pts, pts))
        match("input coverage", [keys["tick"] for keys in row["steps"]], list(range(previous_tick + 1, frame["tick"] + 1)))
        previous_tick = frame["tick"]
        frame_path = f"frames/{index:05d}.png"
        match("frame path", frame["path"], frame_path)
        png = directory / frame_path
        receipt = {"video_sha256": video["sha256"], "label": "observed", "frame_index": index,
                   "pts_ms": pts, "frame_sha256": sha256(png)}
        provenance.append(receipt)
        match("frame hash and provenance", (frame["frame_sha256"], frame["provenance"]),
              (receipt["frame_sha256"], receipt))
        if frame["screen"] != previous_screen:
            transitions.append({"from": previous_screen, "to": frame["screen"], "provenance": receipt})
        previous_screen = frame["screen"]
        with Image.open(png) as image:
            match("raster dimensions", image.size, (width, height))
            check_visuals(frame, image, truth, colors)

    match("screen transitions", truth["screen_transitions"], transitions)
    for event in truth["events"]:
        receipt = provenance[event["frame_index"]]
        match("event provenance", (event["pts_ms"], event["frame_sha256"], event["provenance"]),
              (receipt["pts_ms"], receipt["frame_sha256"], receipt))
    cues = [{"kind": event["kind"], "pts_ms": event["pts_ms"]} for event in truth["events"]
            if event["kind"] in truth["rules"]["audible_events"]]
    match("audio cues", (truth["audio_cues"], truth["audio_cue_pts_ms"]),
          (cues, [cue["pts_ms"] for cue in cues]))
    return check_trajectory(truth, inputs)
