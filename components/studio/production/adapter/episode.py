#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bind, render, reopen and conform digest-pinned registered shots."""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import episode_bind as binding

HERE = Path(__file__).resolve().parent
PRODUCT = HERE.parent
WORKER = HERE / "episode_shot.py"
sys.path.insert(0, str(PRODUCT / "lib/viewpoint-studio"))

from viewpoint.episode_local import common as files, host
from viewpoint.episode_local.episode import command

sys.path.insert(0, str(PRODUCT / "editorial"))
from common import destination, ffmpeg, stamp


def write(path, data):
    files.write(path, data)


def checked_shots(bound, digest):
    binding.need(bound["schema"] == binding.SCHEMA, "bound_schema")
    binding.need(bound["entry_point"]["shot_script"]["sha256"] == binding.sha_file(WORKER), "adapter_changed")
    binding.need(binding.DIGEST.fullmatch(digest), "bound_digest_invalid")
    ids = [shot["id"] for shot in bound["shots"]]
    binding.need(ids and len(ids) == len(set(ids)) and all(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", i)
                 for i in ids), "invalid_shot_ids")
    return bound["shots"]


def run_shot(args, stage, shot, bound, digest):
    work = stage / shot["id"]
    work.mkdir()
    entry = bound["entry_point"]
    producer, = [row for row in bound["companions"] if row["role"] == "set_producer" and row["shot"] == shot["id"]]
    for row in [producer, *shot["assets"].values()]:
        binding.verified(row, shot["id"])
    reopen = args.verb == "reopen"
    original = (args.renders / shot["id"]).resolve(strict=True) if reopen else work
    settings = {"mode": args.verb, "bound_sha256": digest, "shot": shot, "pixels": bound["pixels"],
                "fps": bound["timebase"]["edit_rate_fps"], "samples": entry["samples"],
                "threads": entry["threads"], "material_name": entry["material_name"], "set_producer": producer,
                "frame_dir": str(work / "frames"), "blend": str(original / "shot.blend"),
                "receipt": str(original / "shot-receipt.json"), "reopen_receipt": str(work / "reopen-receipt.json"),
                "reopen_frame": shot["source"]["in"], "reopen_frame_png": str(work / "reopened.png")}
    path = work / "settings.json"
    write(path, settings)
    read_roots = [HERE, *(Path(ref["path"]).resolve().parent for ref in [producer, *shot["assets"].values()])]
    if reopen:
        read_roots.append(original)
    argv = ["/usr/bin/blender", "-b", "--factory-startup", "--disable-autoexec", "-noaudio",
            "--python-exit-code", "2", "--python", WORKER, "--", path]
    result = host.scoped(work, read_roots, [command("blender-" + args.verb, argv, 1800)])
    if result["commands"][-1]["rc"]:
        raise RuntimeError(f"Blender {args.verb} failed. See the scoped command receipt.")
    receipt = binding.document(work / ("reopen-receipt.json" if reopen else "shot-receipt.json"))
    binding.need(receipt["status"] == "PASS" and receipt["bound_sha256"] == digest,
                 receipt.get("reason", "shot_receipt_stale"), shot["id"])
    return receipt


def conform(stage, source, bound, digest, shots):
    frames = stage / "frames"
    frames.mkdir()
    index = 0
    ranges = []
    for shot in shots:
        root = source / shot["id"]
        receipt = binding.document(root / "shot-receipt.json")
        binding.need(receipt["status"] == "PASS" and receipt["bound_sha256"] == digest
                     and receipt["shot"] == shot["id"], "shot_receipt_stale", shot["id"])
        hashes = {row["frame"]: row["sha256"] for row in receipt["frames"]}
        start = index
        for frame in range(shot["source"]["in"], shot["source"]["out"] + 1):
            target = frames / f"{index:04d}.png"
            original = root / "frames" / f"{frame:03d}.png"
            shutil.copyfile(binding.verified({"path": str(original), "sha256": hashes[frame]}, shot["id"]), target)
            binding.need(binding.sha_file(target) == hashes[frame], "shot_frame_changed", original)
            index += 1
        ranges.append({"id": shot["id"], "start": start, "end": index})
    binding.need(index == bound["total_edit_frames"], "edit_length_mismatch", index)
    fps = bound["timebase"]["edit_rate_fps"]
    movie = stage / "episode.mp4"
    ffmpeg("-framerate", fps, "-i", frames / "%04d.png", "-frames:v", index, "-an",
           "-map_metadata", "-1", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", movie)
    captions = binding.caption_plan(bound["cues"], index)
    (stage / "captions.srt").write_text("".join(
        f'{i}\n{stamp(row["start"], fps)} --> {stamp(row["end"] + 1, fps)}\n{row["text"]}\n\n'
        for i, row in enumerate(captions, 1)), encoding="utf-8")
    write(stage / "cues.json", {"bound_sha256": digest, "fps": fps, "cues": bound["cues"]})
    return {"status": "CONFORMED", "bound_sha256": digest, "movie_sha256": binding.sha_file(movie),
            "frames": index, "fps": fps, "shots": ranges}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--library", required=True, type=Path)
    resolve.add_argument("--shot-list", required=True, type=Path)
    resolve.add_argument("--out", required=True, type=Path)
    bind = sub.add_parser("bind")
    bind.add_argument("--library", required=True, type=Path)
    bind.add_argument("--resolved", required=True, type=Path)
    bind.add_argument("--intent", required=True, type=Path)
    bind.add_argument("--material-name", required=True)
    bind.add_argument("--out", required=True, type=Path)
    for verb in ("render", "reopen", "conform"):
        action = sub.add_parser(verb)
        action.add_argument("--bound", required=True, type=Path)
        action.add_argument("--sha256", required=True)
        action.add_argument("--out", required=True, type=Path)
        if verb != "render":
            action.add_argument("--renders", required=True, type=Path)
    args = parser.parse_args()
    if args.verb in {"resolve", "bind"}:
        catalogue = binding.Catalogue(args.library)
        if args.verb == "resolve":
            data = catalogue.run("assemble", str(args.shot_list))
            binding.need(data["snapshot"] == catalogue.snapshot, "catalogue_snapshot_changed")
        else:
            data = binding.bind(args.resolved, args.intent, catalogue, {"shot_script": binding.pin(WORKER),
                                "samples": 64, "threads": 8, "material_name": args.material_name})
        files.write(args.out, data, immutable=True)
        return {"status": args.verb.upper(), "sha256": binding.sha_file(args.out)}
    raw = args.bound.read_bytes()
    digest = binding.digest(raw)
    binding.need(digest == args.sha256, "bound_digest_mismatch", args.bound)
    bound = json.loads(raw)
    shots = checked_shots(bound, digest)
    with destination(args.out) as stage:
        if args.verb == "conform":
            result = conform(stage, args.renders, bound, digest, shots)
        else:
            receipts = [run_shot(args, stage, shot, bound, digest) for shot in shots]
            result = {"status": "RENDERED" if args.verb == "render" else "REOPENED", "shots": receipts}
        write(stage / "receipt.json", result)
    return result


if __name__ == "__main__":
    from common import Refusal

    try:
        print(json.dumps(main(), sort_keys=True))
    except Refusal as exc:
        print(json.dumps(exc.receipt, sort_keys=True))
        sys.exit(1)
    except (binding.Refused, files.Refusal) as exc:
        print(json.dumps({"reason": exc.reason, "retry_when": "after correcting the pinned inputs",
                          "next_command": sys.argv, "detail": str(exc)}, sort_keys=True))
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"reason": "harness_error", "retry_when": "after correcting the input or runtime",
                          "next_command": sys.argv, "detail": str(exc)}, sort_keys=True))
        sys.exit(2)
