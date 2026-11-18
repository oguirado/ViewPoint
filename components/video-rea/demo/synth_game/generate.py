# SPDX-License-Identifier: AGPL-3.0-only
"""Generate a calibrated, variable-rate recording of the Beacon Run input tape."""
import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from .game import Game, HEIGHT, PICKUPS, ROWS, RULES, SCALE, STEP_MS, TILE, WIDTH, WORLD, controls, spawns
from .media import MEDIA_TIMEOUT_S, run_media_tool
from .render import render
from .timing import probe_timing, validate_timing


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encode_movie(stage, frames, duration_ms):
    # All presentation times are on the physics clock; gaps span differing tick counts.
    # Give each image a one-step packet duration, including the final held image.
    manifest = stage / "frames.ffconcat"
    with manifest.open("w", encoding="ascii") as stream:
        stream.write("ffconcat version 1.0\n")
        ends = [frame["pts_ms"] for frame in frames[1:]] + [duration_ms]
        for frame, end in zip(frames, ends, strict=True):
            gap = end - frame["pts_ms"]
            stream.write(f"file '{frame['path']}'\noption framerate 1000/{STEP_MS}\n")
            stream.write(f"duration {gap // 1000}.{gap % 1000:03d}\n")
    run_media_tool("ffmpeg", [
        "-nostdin", "-v", "error", "-protocol_whitelist", "file",
        "-f", "concat", "-safe", "0", "-threads", "1", "-i", manifest.name,
        "-an", "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
        "-c:v", "libx264", "-threads", "1", "-bf", "0", "-pix_fmt", "yuv420p",
        "-fps_mode", "vfr", "-enc_time_base", "1:1000", "-video_track_timescale", "1000",
        "recording.mp4",
    ], cwd=stage, timeout=MEDIA_TIMEOUT_S)
    manifest.unlink()


def write_fixture(stage, seed, randomized):
    (stage / "frames").mkdir()
    game = Game(spawns(seed, randomized))
    frames, events, inputs, steps = [], [], [], []
    gaps = (2, 1, 3, 2, 4)
    sample_tick = 0
    sample_number = 0
    for tick in range(370):
        keys = controls(tick)
        emitted = game.step(keys)
        steps.append(keys)
        scheduled = tick == sample_tick
        if scheduled:
            sample_tick += gaps[sample_number % len(gaps)]
            sample_number += 1
        if not (scheduled or emitted or tick == 369):
            continue
        index = len(frames)
        path = f"frames/{index:05d}.png"
        frame = {**game.snapshot(), "tick": tick, "frame_index": index,
                 "pts_ms": tick * STEP_MS, "path": path}
        render(frame).save(stage / path)
        frame["frame_sha256"] = digest(stage / path)
        frames.append(frame)
        for kind, subject in emitted:
            events.append({"kind": kind, "subject": subject, "frame_index": index,
                           "episode": game.episode, "frame_sha256": frame["frame_sha256"]})
        inputs.append({"frame_index": index, "steps": steps})
        steps = []

    duration_ms = frames[-1]["pts_ms"] + STEP_MS
    encode_movie(stage, frames, duration_ms)
    movie = stage / "recording.mp4"
    timing = probe_timing(movie)
    video = {"path": movie.name, "sha256": digest(movie), "width": WIDTH, "height": HEIGHT,
             "duration_ms": duration_ms, "pts_source": "container frame pts_time",
             "frame_hash_basis": "authored PNG file bytes before video encoding"}
    # The millisecond time base is exact here. Only publish actual container PTS.
    validate_timing({"frames": frames, "video": video}, timing, tolerance_ms=0)
    for frame, row, pts in zip(frames, inputs, timing.pts_ms, strict=True):
        frame["pts_ms"] = row["pts_ms"] = int(pts)
        frame["provenance"] = {
            "video_sha256": video["sha256"], "label": "observed",
            **{key: frame[key] for key in ("frame_index", "frame_sha256", "pts_ms")},
        }
    for event in events:
        frame = frames[event["frame_index"]]
        event["pts_ms"] = frame["pts_ms"]
        event["provenance"] = frame["provenance"]
    audio_cues = [{"kind": e["kind"], "pts_ms": e["pts_ms"]}
                  for e in events if e["kind"] in RULES["audible_events"]]
    truth = {
        "fixture": "Beacon Run", "randomized": randomized, "spawn_seed": seed if randomized else None,
        "video": video,
        "physics": {"world": WORLD, "pixels_per_metre": SCALE,
                    "scale_uncertainty_px_per_m": 0, "fixed_step_ms": STEP_MS,
                    "screen": {"gravity_px_s2": WORLD["gravity_m_s2"] * SCALE,
                               "run_speed_px_s": WORLD["run_speed_m_s"] * SCALE,
                               "jump_impulse_px_s": WORLD["jump_impulse_m_s"] * SCALE,
                               "friction_px_s2": WORLD["friction_m_s2"] * SCALE},
                    "axes": "Level origin top left, positive x right, positive y down. Player anchor at feet.",
                    "integration": "Set run speed or apply coast friction, then jump, gravity, position, floor contact."},
        "rules": RULES,
        "level": {"tile_px": TILE, "rows": ROWS, "legend": {".": "air", "#": "solid"}},
        "enemy_spawns": game.enemies, "pickup_centres_px": {k: [x, RULES["floor_y_px"] - 7] for k, x in PICKUPS.items()},
        "inputs": "inputs.jsonl", "frames": frames, "events": events,
        "audio_cues": audio_cues, "audio_cue_pts_ms": [cue["pts_ms"] for cue in audio_cues],
    }
    (stage / "ground_truth.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    with (stage / "inputs.jsonl").open("w", encoding="utf-8") as output:
        for row in inputs:
            output.write(json.dumps(row, separators=(",", ":")) + "\n")


def generate(out, seed, randomized=False):
    out = Path(out).absolute()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        raise ValueError("A new or empty output directory is required")
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".synth-game-") as temporary:
        stage = Path(temporary) / "recording"
        stage.mkdir()
        write_fixture(stage, seed, randomized)
        stage.replace(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--random", action="store_true", help="Vary enemy spawns from the seed")
    args = parser.parse_args(argv)
    generate(args.out, args.seed, args.random)


if __name__ == "__main__":
    main()
