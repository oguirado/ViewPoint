# SPDX-License-Identifier: AGPL-3.0-only

"""Render a seeded desktop session and its authored labels without a display."""

import argparse
import hashlib
import json
import random
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import ImageFont

from .ground_truth import (
    frame_witness, probe_timing, transition_graph, validate_ground_truth, validate_timing,
)
from .media import run_media_tool
from .scene import EXITS, HEIGHT, PARENTS, WIDTH, State, render, scene

FINAL_HOLD_MS = 240
RECORDING = "recording.mp4"


@dataclass(frozen=True)
class Step:
    target: str
    kind: str = "click"
    text: str | None = None


WORKFLOWS = (
    ("edit_note", (
        Step("scroll_down", "cursor_move"),
        Step("scroll_down"),
        Step("project", "cursor_move"),
        Step("project"),
        Step("save", "cursor_move"),
        Step("save"),
        Step("notes", "cursor_move"),
        Step("notes"),
        Step("notes", "type", text="Check the survey"),
        Step("notes", "type", text=" on Friday"),
        Step("save", "cursor_move"),
        Step("save"),
    )),
    ("change_settings", (
        Step("file_menu", "cursor_move"),
        Step("file_menu"),
        Step("settings_item", "cursor_move"),
        Step("settings_item"),
        Step("workspace", "cursor_move"),
        Step("workspace"),
        Step("workspace", "type", text=" West"),
        Step("apply", "cursor_move"),
        Step("apply"),
    )),
    ("review_and_cancel_archive", (
        Step("project", "cursor_move"),
        Step("project"),
        Step("archive", "cursor_move"),
        Step("archive"),
        Step("cancel_archive", "cursor_move"),
        Step("cancel_archive"),
        Step("back", "cursor_move"),
        Step("back"),
    )),
)


def sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def render_session(out: Path, seed: int) -> tuple[list[dict], list[dict], list[dict]]:
    rng = random.Random(seed)
    font = ImageFont.load_default(size=16)
    state = State(f"Kepler survey {rng.randrange(100, 1000)}",
                  f"North desk {rng.randrange(1, 20)}", rng.randint(18, 42))
    frames, actions, workflows = [], [], []
    screen, pointer = "main", (606, 325)
    pts = 0

    def snapshot(role, *, gap=0, emphasis="", hover=None, action=None, cause=None):
        nonlocal pts
        pts += gap
        image, widgets = render(screen, state, pointer, emphasis, hover, font)
        index = len(frames)
        path = Path("frames") / f"frame_{index:06d}.png"
        image.save(out / path)
        frames.append({
            "frame_index": index, "pts_ms": pts, "frame_role": role,
            "pointer": pointer, "screen_id": screen, "parent_screen": PARENTS.get(screen),
            "hover_target": hover, "scroll_offset_px": state.scroll_offset,
            "action_id": action["id"] if action else None,
            "action_target": action["target"] if action else None,
            "cause_action_id": cause, "path": path.as_posix(),
            "frame_sha256": sha256_file(out / path), "widgets": widgets,
        })

    (out / "frames").mkdir()
    snapshot("initial")
    # Both workflows start and end on main, so either can precede the other.
    flows = WORKFLOWS if seed % 2 == 0 else (WORKFLOWS[1], WORKFLOWS[0], WORKFLOWS[2])
    for flow_id, steps in flows:
        action_ids = []
        for step in steps:
            target = next(w for w in scene(screen, state) if w.id == step.target)
            if step.kind == "cursor_move":
                start = pointer
                for tick in (1, 2, 3):
                    pointer = tuple(a + (b-a)*tick//4 for a, b in zip(start, target.center))
                    snapshot("motion", gap=rng.randint(21, 64))
            pointer = target.center
            action = {"id": f"action_{len(actions) + 1:03d}", "kind": step.kind,
                      "target": step.target, "pointer": pointer, "typed_text": step.text,
                      "value_after": None, "text": None}
            if step.kind == "type":
                value = getattr(state, step.target) + step.text
                setattr(state, step.target, value)
                action.update(value_after=value, text=value)
            if step.kind == "click":
                if target.id == "scroll_down":
                    state.scroll_offset += 6
                route = EXITS[screen].get(target.id)
                if route and target.enabled:
                    screen = route[0]
                    if target.id == "save":
                        state.status = "Note saved"
            snapshot("action", gap=rng.randint(90, 310), emphasis=step.kind, action=action)
            action.update(pts_ms=pts, frame_index=len(frames)-1)
            actions.append(action)
            action_ids.append(action["id"])
            if step.kind == "cursor_move":
                snapshot("hover", gap=rng.randint(60, 130), hover=target.id, cause=action["id"])
                snapshot("hold", gap=rng.randint(85, 200), hover=target.id)
            elif step.kind == "click" and target.id == "scroll_down":
                for offset in (12, 18):
                    state.scroll_offset = offset
                    snapshot("scroll", gap=rng.randint(33, 70), cause=action["id"])
        workflows.append({"id": flow_id, "action_ids": action_ids})
    snapshot("settle", gap=rng.randint(90, 180))
    snapshot("hold", gap=rng.randint(90, 180))
    for current, following in zip(frames, frames[1:]):
        current["duration_ms"] = following["pts_ms"] - current["pts_ms"]
    frames[-1]["duration_ms"] = FINAL_HOLD_MS
    return frames, actions, workflows


def encode(out: Path, frames: list[dict]) -> None:
    concat = out / "frames.ffconcat"
    with concat.open("w", encoding="ascii", newline="\n") as manifest:
        manifest.write("ffconcat version 1.0\n")
        for frame in frames:
            manifest.write(f"file '{frame['path']}'\noption framerate 1000\n")
            gap = frame["duration_ms"]
            manifest.write(f"duration {gap // 1000}.{gap % 1000:03d}\n")

    # Only the manifest above supplies paths. The demuxer requires safe=0 for option lines.
    final_packet = len(frames) - 1
    hold = frames[-1]["duration_ms"]
    run_media_tool(
        "ffmpeg",
        [
            "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file",
            "-f", "concat", "-safe", "0", "-threads", "1", "-i", concat.name,
            "-an", "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
            "-c:v", "libx264", "-threads", "1", "-bf", "0", "-pix_fmt", "yuv420p",
            "-fps_mode", "vfr", "-enc_time_base", "1:1000", "-video_track_timescale", "1000",
            # Concat sets successive PTS; the last packet has no following PTS.
            "-bsf:v", rf"setts=duration=if(eq(N\,{final_packet})\,{hold}\,DURATION)",
            RECORDING,
        ],
        cwd=out,
    )


def generate(out: Path, seed: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".synth-gui-", dir=out) as temporary:
        stage = Path(temporary)
        if any(entry != stage for entry in out.iterdir()):
            raise ValueError("The output directory must be empty")
        frames, actions, workflows = render_session(stage, seed)
        duration_ms = frames[-1]["pts_ms"] + frames[-1]["duration_ms"]
        encode(stage, frames)
        timing = probe_timing(stage / RECORDING)
        video_sha256 = sha256_file(stage / RECORDING)
        for action in actions:
            action.update(
                provenance=frame_witness(video_sha256, frames[action["frame_index"]]),
                target_element_sha256=None, confidence=1.0,
            )
        truth = {
            "seed": seed,
            "video": {
                "path": RECORDING, "sha256": video_sha256,
                "width": WIDTH, "height": HEIGHT, "duration_ms": duration_ms,
            },
            "frames": frames,
            "actions": actions,
            "transition_graph": transition_graph(frames),
            "workflows": workflows,
        }
        validate_ground_truth(truth)
        validate_timing(truth, timing)
        (stage / "ground_truth.json").write_text(
            json.dumps(truth, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n",
        )
        for name in ("frames", RECORDING, "ground_truth.json"):
            (stage / name).rename(out / name)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seed", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        generate(args.out, args.seed)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
