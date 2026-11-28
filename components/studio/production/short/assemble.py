#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Assemble pinned, handled shots with a frame-locked mix and captions."""
from fractions import Fraction
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "editorial"))
from common import Parser, destination, digest, dump, ffmpeg, load, probe, require, run, valid_slug
from conform import media_path
from sound import subtitle_text

RETRY = "supply a validated timeline and the original pinned media"


def track(path, kind):
    tracks = probe(path)["streams"]
    require(len(tracks) == 1 and tracks[0]["codec_type"] == kind, "invalid_" + kind, RETRY)
    return tracks[0]


def assemble(edit, root, output):
    fps = edit["fps"]
    require(edit["schema"] == "viewpoint.short-edit/v1" and type(fps) is int and 1 <= fps <= 60, "invalid_edit", RETRY)
    pixels = edit["pixels"]
    require(isinstance(pixels, list) and len(pixels) == 2
            and all(type(value) is int and 2 <= value <= 8192 and value % 2 == 0 for value in pixels),
            "invalid_picture_size", RETRY)
    require(isinstance(edit["shots"], list) and 1 <= len(edit["shots"]) <= 100, "invalid_shot_count", RETRY)
    sources, filters, cuts, seen = [], [], [], set()
    cursor = 0
    for index, shot in enumerate(edit["shots"]):
        name = shot["id"]
        start, end = shot["source_cut"]
        require(valid_slug(name) and name not in seen and type(start) is int and type(end) is int
                and 0 <= start < end, "invalid_shot", RETRY)
        seen.add(name)
        source = media_path(root, shot["media"], "stale_picture")
        video = track(source, "video")
        require([video["width"], video["height"]] == pixels
                and Fraction(video["avg_frame_rate"]) == fps, "source_clock_mismatch", RETRY)
        sources += ["-i", source]
        filters.append(f"[{index}:v]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,setsar=1[v{index}]")
        cuts.append({"id": name, "start": cursor, "end": cursor + end - start})
        cursor += end - start

    captions = edit["captions"]
    last = 0
    require(isinstance(captions, list) and bool(captions), "captions_required", RETRY)
    for row in captions:
        start, end = row["start"], row["end"]
        text = row["text"]
        require(type(start) is int and type(end) is int and last <= start < end <= cursor
                and isinstance(text, str) and text.strip() == text and text and "\n" not in text and "\r" not in text,
                "invalid_caption", RETRY)
        last = end
    srt = output / "captions.srt"
    srt.write_text(subtitle_text(captions, fps), encoding="utf-8")

    mix = media_path(root, edit["mix"], "stale_audio")
    audio = track(mix, "audio")
    require(Fraction(audio["time_base"]) * int(audio["duration_ts"]) == Fraction(cursor, fps),
            "audio_clock_mismatch", RETRY)
    count = len(cuts)
    filters.append("".join(f"[v{i}]" for i in range(count)) + f"concat=n={count}:v=1:a=0[v]")
    picture = output / "picture-lossless.mkv"
    ffmpeg(*sources, "-filter_complex", ";".join(filters), "-map", "[v]",
           "-r", fps, "-c:v", "ffv1", "-threads", "1", "-map_metadata", "-1", "-fflags", "+bitexact", picture)
    movie = output / "master.mp4"
    keyframes = (["-force_key_frames", "expr:" + "+".join(f'eq(n,{row["start"]})' for row in cuts)] if count > 1 else [])
    ffmpeg("-i", picture, "-i", mix, "-i", srt, "-map", "0:v", "-map", "1:a", "-map", "2:s",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-bf", "0", "-threads", "1", *keyframes,
           "-c:a", "aac", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
           "-map_metadata", "-1", "-fflags", "+bitexact", "-movflags", "+faststart", movie)
    tracks = probe(movie)["streams"]
    require([row["codec_type"] for row in tracks] == ["video", "audio", "subtitle"]
            and int(tracks[0]["nb_frames"]) == cursor and Fraction(tracks[0]["avg_frame_rate"]) == fps,
            "delivery_clock_mismatch", RETRY)
    ffmpeg("-xerror", "-i", movie, "-map", "0:v", "-map", "0:a", "-f", "null", "-")
    for shot in edit["shots"]:
        media_path(root, shot["media"], "stale_picture")
    media_path(root, edit["mix"], "stale_audio")
    return {"status": "ASSEMBLED", "fps": fps, "frames": cursor, "shots": cuts,
            "files": {file.name: digest(file) for file in (movie, picture, srt)}}


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    with destination(args.out) as stage:
        receipt = assemble(load(args.timeline), args.timeline.parent, stage)
        dump(stage / "receipt.json", receipt)
    return receipt


if __name__ == "__main__":
    raise SystemExit(run(main))
