# SPDX-License-Identifier: AGPL-3.0-only
"""Conform handled source clips according to an editable, digest-bound timeline."""
import re
from pathlib import Path

from common import Parser, destination, digest, dump, ffmpeg, load, path_in, probe, require, run, valid_slug
from sound import subtitle_text

CONFORM = "python3 -B conform.py --timeline TIMELINE --out CONFORM"
SHA256 = re.compile(r"[a-f0-9]{64}")


def timeline(block, sources, sound):
    shots = []
    for shot, media in zip(block["shots"], sources, strict=True):
        start, end = shot["edit"]
        shots.append({"id": shot["id"], "edit_start": start, "edit_end": end,
                      "source": shot["source"], "cut": shot["cut"], "handles": shot["handles"],
                      "transition_in": shot["transition_in"], "camera": shot["camera"], "light": shot["light"],
                      "cast": {actor: row["asset_sha256"] for actor, row in shot["cast"].items()},
                      "media": {"path": media["path"], "sha256": media["sha256"]}})
    return {"schema": "rp4.editorial-timeline/v1", "id": block["id"], "title": block["title"],
            "fps": block["fps"], "pixels": block["pixels"], "total_edit_frames": block["total_edit_frames"],
            "clocks": {"edit": "zero-based half-open", "source": "one-based inclusive"},
            "shots": shots, "mix": sound["mix"], "subtitle": sound["subtitle"], "stems": sound["stems"],
            "language": sound["language"], "captions": sound["captions"], "sample_rate": sound["sample_rate"]}


def media_path(root, ref, reason):
    path = path_in(root, ref["path"])
    require(SHA256.fullmatch(ref["sha256"]) and path.is_file() and digest(path) == ref["sha256"],
            reason, "restore the media named by its digest or regenerate the editorial bundle", CONFORM)
    return path


def conform(edit, root, output):
    require(edit["schema"] == "rp4.editorial-timeline/v1" and type(edit["fps"]) is int and 1 <= edit["fps"] <= 60,
            "unsupported_timeline", "use the editorial timeline and a valid integer rate", CONFORM)
    require(bool(edit["shots"]) and len({s["id"] for s in edit["shots"]}) == len(edit["shots"]),
            "invalid_edit", "provide at least one shot with unique IDs", CONFORM)
    args, filters = [], []
    cursor = 0
    cast = edit["shots"][0]["cast"]
    for i, shot in enumerate(edit["shots"]):
        start, end = shot["edit_start"], shot["edit_end"]
        first, last = shot["cut"]
        source_in, source_out = shot["source"]
        require(valid_slug(shot["id"]) and all(type(n) is int for n in (start, end, first, last, source_in, source_out))
                and source_in == 1 and source_in <= first <= last <= source_out
                and start == cursor and end - start == last - first + 1
                and shot["handles"] == [first - source_in, source_out - last]
                and shot["transition_in"] == ("cut" if i else "start"),
                "invalid_edit", "reconcile source trims with contiguous edit ranges", CONFORM)
        require(len(cast) == 2 and cast == shot["cast"], "wrong_actor", "keep both actor bindings fixed across edits", CONFORM)
        source = media_path(root, shot["media"], "stale_picture")
        tracks = probe(source)["streams"]
        require(len(tracks) == 1 and tracks[0]["codec_type"] == "video"
                and [tracks[0]["width"], tracks[0]["height"]] == edit["pixels"]
                and tracks[0]["r_frame_rate"] == f'{edit["fps"]}/1',
                "invalid_source_media", "render each shot with the approved dimensions and clock", CONFORM)
        args += ["-i", source]
        filters.append(
            f"[{i}:v]trim=start_frame={first-1}:end_frame={last},setpts=PTS-STARTPTS,setsar=1[v{i}]"
        )
        cursor = end
    require(cursor == edit["total_edit_frames"], "invalid_edit", "reconcile the cut with the total edit duration", CONFORM)
    captions = media_path(root, edit["subtitle"], "stale_subtitle")
    require(captions.read_text(encoding="utf-8") == subtitle_text(edit["captions"], edit["fps"]),
            "stale_subtitle", "regenerate the English SRT from the cut's caption intervals", CONFORM)
    paths = [media_path(root, item, "stale_audio") for item in edit["stems"].values()]
    mix = media_path(root, edit["mix"], "stale_audio")
    mix_track = probe(mix)["streams"][0]
    require(mix_track["codec_type"] == "audio" and int(mix_track["sample_rate"]) == edit["sample_rate"]
            and int(mix_track["duration_ts"]) * edit["fps"] == cursor * edit["sample_rate"],
            "invalid_mix_clock", "rebuild the frame-locked mix after editing", CONFORM)
    n = len(filters)
    filters.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[out]")
    ffmpeg(*args, "-i", mix, "-i", captions, "-filter_complex", ";".join(filters),
           "-map", "[out]", "-map", f"{n}:a", "-map", f"{n+1}:s",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "17", "-threads", "1", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
           "-map_metadata", "-1", "-fflags", "+bitexact", "-movflags", "+faststart", output)
    tracks = probe(output)["streams"]
    require([track["codec_type"] for track in tracks] == ["video", "audio", "subtitle"]
            and int(tracks[0]["nb_frames"]) == cursor,
            "wrong_frame_count", "provide the full declared source ranges and recheck the edit", CONFORM)
    return {"status": "CONFORMED", "sha256": digest(output), "shots": n, "stems": len(paths),
            "frames": cursor, "fps": edit["fps"], "pixels": edit["pixels"]}


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    edit = load(args.timeline)
    with destination(args.out) as stage:
        receipt = conform(edit, args.timeline.parent, stage / "master.mp4")
        dump(stage / "conform.json", receipt)
    return receipt


if __name__ == "__main__":
    raise SystemExit(run(main))
