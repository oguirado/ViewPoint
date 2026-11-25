#!/usr/bin/env python3
"""Compile and reopen a project-relative storyboard and its pinned shot schedule."""

import argparse
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
import errno
from fractions import Fraction
from functools import cache
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from tempfile import TemporaryDirectory

from jsonschema import Draft202012Validator, validators


MAX_SHOT_FRAMES = 600
NATIVE_MIN_FRAMES = 2
SCHEMA_PATH = Path(__file__).with_name("schema.json")
MANIFEST_VERSIONS = {
    "storyboard/v1": "story-shot-manifest/v1",
    "storyboard/v2": "story-shot-manifest/v2",
}
CUE_KINDS = ("caption", "vo")


class StoryError(ValueError):
    """Named rejection of storyboard, asset or schedule data."""

    def __init__(self, cause, detail):
        self.cause = cause
        super().__init__(f"{cause}: {detail}")


def require(condition, cause, detail):
    if not condition:
        raise StoryError(cause, detail)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key", repr(key))
        result[key] = value
    return result


def reject_constant(value):
    raise StoryError("invalid_json_number", value)


def decode_json(raw):
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except StoryError:
        raise
    except (ValueError, RecursionError) as exc:
        raise StoryError("invalid_json", str(exc)) from exc


def canonical_bytes(value):
    """Stable UTF-8 JSON with the factory convention of one trailing LF."""
    try:
        compact = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        return (compact + "\n").encode("utf-8")
    except UnicodeError as exc:
        raise StoryError("invalid_json_string", "expected Unicode scalar values") from exc


def json_pin(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@cache
def storyboard_validator():
    schema = json.loads(SCHEMA_PATH.read_bytes())
    Draft202012Validator.check_schema(schema)
    integer_checker = Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda _, value: type(value) is int
    )
    return validators.extend(Draft202012Validator, type_checker=integer_checker)(schema)


def ordered_unique(rows, key, cause):
    ordered = sorted(rows, key=key)
    for previous, current in zip(ordered, ordered[1:]):
        require(key(previous) != key(current), cause, str(key(current)))
    return ordered


def by_id(row):
    return row["id"]


def by_interval(row):
    return row["start_frame"], row["end_frame"], row["id"]


def validate_interval(row, start, end, kind):
    first, last = row["start_frame"], row["end_frame"]
    require(first < last, f"invalid_{kind}_interval", row["id"])
    require(start <= first < last <= end, f"{kind}_outside_timeline", row["id"])


def validate_partitions(scene):
    scene_id = scene["id"]
    frontier = scene["start_frame"]
    scene_end = scene["end_frame"]
    minimum = NATIVE_MIN_FRAMES if scene["backend"] in ("native", "unassigned") else 1
    for part in scene["partitions"]:
        first, last = part["start_frame"], part["end_frame"]
        require(first >= frontier, "partition_overlap", f"{scene_id}: starts at {first}, expected {frontier}")
        require(first == frontier, "partition_gap", f"{scene_id}: starts at {first}, expected {frontier}")
        require(first < last <= scene_end, "invalid_partition_interval", f"{scene_id}: [{first}, {last})")
        require(
            minimum <= last - first <= MAX_SHOT_FRAMES,
            "invalid_partition_length",
            f"{scene_id}: [{first}, {last}) requires {minimum}–{MAX_SHOT_FRAMES} frames",
        )
        frontier = last
    require(frontier == scene_end, "partition_gap", f"{scene_id}: ends at {frontier}, expected {scene_end}")


def validate_scenes(board, source_ids, surface_ids):
    start, end = board["start_frame"], board["end_frame"]
    scenes = ordered_unique(board["scenes"], by_id, "duplicate_scene_id")
    frontier = start
    for scene in sorted(scenes, key=by_interval):
        validate_interval(scene, start, end, "scene")
        first, last = scene["start_frame"], scene["end_frame"]
        scene_id = scene["id"]
        require(first >= frontier, "scene_overlap", f"{scene_id} starts at {first}, expected {frontier}")
        require(first == frontier, "scene_gap", f"{scene_id} starts at {first}, expected {frontier}")
        if "partitions" in scene:
            validate_partitions(scene)
        elif scene["backend"] == "native":
            require(last - first >= NATIVE_MIN_FRAMES, "native_scene_too_short", scene_id)
        for field, known in (("source_ids", source_ids), ("surface_ids", surface_ids)):
            unknown = sorted(set(scene[field]) - known)
            require(not unknown, "unknown_asset_reference", f"{scene_id}.{field}: {unknown}")
        frontier = last
    require(frontier == end, "scene_gap", f"coverage ends at {frontier}, expected {end}")


def validate_cues(board, source_ids):
    start, end = board["start_frame"], board["end_frame"]
    prefix = f"urn:story:{board['id']}:cue:"
    cues = ordered_unique(board["cues"], by_id, "duplicate_cue_id")
    track_ends = dict.fromkeys(CUE_KINDS, start)
    for cue in sorted(cues, key=by_interval):
        validate_interval(cue, start, end, "cue")
        cue_id, kind = cue["id"], cue["kind"]
        require(cue_id.startswith(prefix), "cue_id_namespace_mismatch", cue_id)
        require(cue["text"].strip(), "empty_cue_text", cue_id)
        require(
            cue["start_frame"] >= track_ends[kind], "cue_overlap", f"{kind}: {cue_id}"
        )
        if "source_id" in cue:
            require(cue["source_id"] in source_ids, "unknown_asset_reference", cue_id)
        track_ends[kind] = cue["end_frame"]


def normalize_storyboard(board):
    error = next(storyboard_validator().iter_errors(board), None)
    if error:
        raise StoryError("invalid_storyboard", f"{error.json_path}: {error.message}")

    fps = Fraction(*board["fps"])
    require(
        1 <= fps <= 240 and [fps.numerator, fps.denominator] == board["fps"],
        "invalid_frame_rate",
        "expected a reduced rate in [1, 240]",
    )
    start, end = board["start_frame"], board["end_frame"]
    require(start < end, "invalid_timeline", f"[{start}, {end})")

    sources = ordered_unique(board["sources"], by_id, "duplicate_source_id")
    surfaces = ordered_unique(board["surfaces"], by_id, "duplicate_surface_id")
    source_ids = {row["id"] for row in sources}
    validate_scenes(board, source_ids, {row["id"] for row in surfaces})
    validate_cues(board, source_ids)

    cuts = ordered_unique(board["camera_cuts"], lambda row: row["frame"], "duplicate_camera_cut")
    require(cuts[0]["frame"] == start, "initial_camera_cut_missing", str(start))
    cameras = set(board["cameras"])
    for cut in cuts:
        require(start <= cut["frame"] < end, "camera_cut_outside_timeline", str(cut["frame"]))
        require(cut["camera"] in cameras, "unknown_camera", cut["camera"])

    scenes = [
        {
            **scene,
            "source_ids": sorted(scene["source_ids"]),
            "surface_ids": sorted(scene["surface_ids"]),
        }
        for scene in sorted(board["scenes"], key=by_interval)
    ]
    return {
        **board,
        "sources": sources,
        "surfaces": surfaces,
        "scenes": scenes,
        "cues": sorted(board["cues"], key=by_interval),
        "cameras": sorted(cameras),
        "camera_cuts": cuts,
    }


@contextmanager
def open_project_directory(directory, *, dir_fd=None):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def asset_parts(asset_path):
    parts = asset_path.split("/")
    require(
        all(part not in ("", ".", "..") for part in parts)
        and not any(ch in asset_path for ch in ("\\", ":", "\0", "\r", "\n")),
        "invalid_asset_path",
        repr(asset_path),
    )
    return parts


def file_digest_at(root_fd, parts):
    """Open each component relative to a pinned directory and refuse symlinks."""
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            os.close(parent_fd)
            parent_fd = child_fd
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
        )
        with os.fdopen(fd, "rb") as stream:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "invalid_asset_file", "/".join(parts))
            return hashlib.file_digest(stream, "sha256").hexdigest()
    finally:
        os.close(parent_fd)


def verify_assets(board, project_root):
    try:
        root = Path(project_root).resolve(strict=True)
        with open_project_directory(root) as root_fd:
            for asset in (*board["sources"], *board["surfaces"]):
                parts = asset_parts(asset["path"])
                try:
                    actual = file_digest_at(root_fd, parts)
                except OSError as exc:
                    cause = "asset_not_found" if exc.errno == errno.ENOENT else "asset_path_unreadable"
                    raise StoryError(cause, f"{asset['id']}: {asset['path']}") from exc
                require(
                    actual == asset["sha256"],
                    "asset_digest_mismatch",
                    f"{asset['id']}: expected {asset['sha256']}, got {actual}",
                )
    except (OSError, RuntimeError) as exc:
        raise StoryError("invalid_project_root", str(project_root)) from exc


def shot_ranges(scene):
    if "partitions" in scene:
        for part in scene["partitions"]:
            yield part["start_frame"], part["end_frame"]
        return

    start, end = scene["start_frame"], scene["end_frame"]
    minimum = NATIVE_MIN_FRAMES if scene["backend"] == "native" else 1
    while end - start > MAX_SHOT_FRAMES:
        stop = min(start + MAX_SHOT_FRAMES, end - minimum)
        yield start, stop
        start = stop
    yield start, end


def frame_boundaries(frame_count, fps):
    times = (offset / fps for offset in range(frame_count + 1))
    return [[time.numerator, time.denominator] for time in times]


def shots_for_storyboard(board):
    source_by_id = {row["id"]: row for row in board["sources"]}
    surface_by_id = {row["id"]: row for row in board["surfaces"]}
    cuts = board["camera_cuts"]
    cut_frames = [cut["frame"] for cut in cuts]
    fps = Fraction(*board["fps"])
    cue_tracks = []
    for kind in CUE_KINDS:
        cues = [cue for cue in board["cues"] if cue["kind"] == kind]
        cue_tracks.append(([cue["end_frame"] for cue in cues], cues))

    for scene in board["scenes"]:
        for part, (start, end) in enumerate(shot_ranges(scene), 1):
            schedule = frame_boundaries(end - start, fps)
            active = bisect_right(cut_frames, start) - 1
            local_cuts = [{"frame": start, "camera": cuts[active]["camera"], "at": schedule[0]}]
            for cut in cuts[active + 1:bisect_left(cut_frames, end)]:
                local_cuts.append({**cut, "at": schedule[cut["frame"] - start]})

            shot_cues = []
            for cue_ends, cues in cue_tracks:
                for index in range(bisect_right(cue_ends, start), len(cues)):
                    cue = cues[index]
                    if cue["start_frame"] >= end:
                        break
                    shot_cues.append(cue)

            source_ids = set(scene["source_ids"])
            source_ids.update(cue["source_id"] for cue in shot_cues if "source_id" in cue)
            yield {
                "id": f"{scene['id']}.part-{part:04d}",
                "scene_id": scene["id"],
                "backend": scene["backend"],
                "fps": board["fps"],
                "start_frame": start,
                "end_frame": end,
                "camera_cuts": local_cuts,
                "frame_schedule": schedule,
                "cue_ids": [cue["id"] for cue in sorted(shot_cues, key=by_interval)],
                "sources": [source_by_id[asset_id] for asset_id in sorted(source_ids)],
                "surfaces": [surface_by_id[asset_id] for asset_id in scene["surface_ids"]],
            }


def compile_storyboard(board, project_root):
    """Return a canonicalizable manifest after checking all authored assets."""
    board = normalize_storyboard(board)
    board_sha = json_pin(board)
    verify_assets(board, project_root)
    shots = list(shots_for_storyboard(board))
    schedule_fields = ("id", "fps", "start_frame", "end_frame", "camera_cuts", "frame_schedule", "cue_ids")
    cue_timing_fields = ("id", "kind", "start_frame", "end_frame")
    schedule = {
        "shots": [{key: shot[key] for key in schedule_fields} for shot in shots],
        "cues": [{key: cue[key] for key in cue_timing_fields} for cue in board["cues"]],
    }
    if board["schema"] == "storyboard/v2":
        schedule.update({key: board[key] for key in ("id", "canvas", "locale", "start_frame", "end_frame")})
    return {
        "schema": MANIFEST_VERSIONS[board["schema"]],
        "storyboard": board,
        "shots": shots,
        "pins": {
            "storyboard_sha256": board_sha,
            "shots_sha256": json_pin(shots),
            "schedule_sha256": json_pin(schedule),
        },
    }


def verify_schedule_pin(manifest, expected_sha256):
    require(
        re.fullmatch(r"[a-f0-9]{64}", expected_sha256),
        "invalid_schedule_pin",
        "expected lowercase SHA-256",
    )
    require(
        manifest["pins"]["schedule_sha256"] == expected_sha256,
        "schedule_digest_mismatch",
        "compiled schedule differs from the supplied pin",
    )


def validate_manifest(raw, project_root, expected_sha256=None, expected_schedule_sha256=None):
    """Rebuild expected shots from the embedded board and verify exact bytes."""
    manifest = decode_json(raw)
    require(
        isinstance(manifest, dict)
        and isinstance(manifest.get("schema"), str)
        and manifest["schema"] in MANIFEST_VERSIONS.values()
        and "storyboard" in manifest,
        "invalid_manifest",
        "expected a supported story-shot-manifest schema",
    )
    if expected_sha256 is not None:
        require(
            re.fullmatch(r"[a-f0-9]{64}", expected_sha256),
            "invalid_manifest_pin",
            "expected lowercase SHA-256",
        )
        require(
            hashlib.sha256(raw).hexdigest() == expected_sha256,
            "manifest_digest_mismatch",
            "file bytes differ from the supplied pin",
        )
    require(raw == canonical_bytes(manifest), "noncanonical_manifest", "use the compiler output")
    expected = compile_storyboard(manifest["storyboard"], project_root)
    require(raw == canonical_bytes(expected), "schedule_mismatch", "manifest differs from its rebuilt storyboard")
    if manifest["schema"] == MANIFEST_VERSIONS["storyboard/v2"]:
        require(
            expected_schedule_sha256 is not None,
            "schedule_pin_required",
            "storyboard/v2 requires an independently supplied schedule pin",
        )
    if expected_schedule_sha256 is not None:
        verify_schedule_pin(manifest, expected_schedule_sha256)
    return manifest


def write_atomic(output, raw):
    with TemporaryDirectory(dir=output.parent, prefix=f".{output.name}.") as directory:
        temporary = Path(directory) / "manifest"
        with temporary.open("xb") as handle:
            handle.write(raw)
        temporary.replace(output)


def protect_inputs(args, storyboard):
    output = args.output.resolve()
    root = args.project_root.resolve()
    require(output != args.board.resolve(), "output_aliases_input", str(output))
    for asset in (*storyboard["sources"], *storyboard["surfaces"]):
        require(output != root / asset["path"], "output_aliases_input", asset["id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    compile_parser = modes.add_parser("compile", help="validate a board and write a shot manifest")
    compile_parser.add_argument("board", type=Path)
    compile_parser.add_argument("--project-root", required=True, type=Path)
    compile_parser.add_argument("--output", required=True, type=Path)
    compile_parser.add_argument("--expect-schedule-sha256")
    validate_parser = modes.add_parser("validate", help="reopen and verify a generated manifest")
    validate_parser.add_argument("manifest", type=Path)
    validate_parser.add_argument("--project-root", required=True, type=Path)
    validate_parser.add_argument("--expect-sha256")
    validate_parser.add_argument("--expect-schedule-sha256")
    args = parser.parse_args()

    try:
        if args.mode == "compile":
            board = decode_json(args.board.read_bytes())
            manifest = compile_storyboard(board, args.project_root)
            if args.expect_schedule_sha256 is not None:
                verify_schedule_pin(manifest, args.expect_schedule_sha256)
            protect_inputs(args, manifest["storyboard"])
            write_atomic(args.output, canonical_bytes(manifest))
        else:
            manifest = validate_manifest(
                args.manifest.read_bytes(), args.project_root, args.expect_sha256, args.expect_schedule_sha256
            )
    except StoryError as exc:
        print(exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"io_error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(manifest["pins"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
