# SPDX-License-Identifier: AGPL-3.0-only
"""Inspect the actual decoded editorial master against its editable sources."""
import wave

import numpy as np

from blocking import project_point, resolve
from common import digest, ffmpeg, load, probe, project_snapshot, require
from conform import media_path, timeline
from sound import cues, subtitle_text

VERIFY = "python3 -B check.py inspect --bundle DELIVERY"
W, H = 160, 90


def demand(condition, reason, fix):
    require(condition, reason, fix, VERIFY)


def pictures(path):
    raw = ffmpeg("-xerror", "-i", path, "-map", "0:v:0", "-vf", f"scale={W}:{H}:flags=area",
                 "-pix_fmt", "rgb24", "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1")
    return np.frombuffer(raw, dtype=np.uint8).reshape((-1, H, W, 3))


def verify_picture(edit, root, movie):
    expected = []
    shots = []
    for shot in edit["shots"]:
        source = media_path(root, shot["media"], "stale_picture")
        frames = pictures(source)
        demand(len(frames) == shot["source"][1], "source_frame_count", "restore the full shot including its handles")
        first, last = shot["cut"]
        expected.append(frames[first - 1:last])
        shots.append({"id": shot["id"], "source_frames": len(frames), "cut_frames": last - first + 1})
    master = pictures(movie)
    demand(len(master) == edit["total_edit_frames"], "wrong_frame_count", "restore all edit frames in the master")
    error = np.abs(master.astype(np.int16) - np.concatenate(expected)).mean(axis=(1, 2, 3))
    demand(float(error.max()) < 3.0, "picture_order_or_content",
           "reconform from the pinned source frames in the published shot order")
    return master, {"frames": len(master), "max_frame_rgb_mae": round(float(error.max()), 4), "shots": shots}


def rgb_window(frame, xy, radius=2):
    x, y = [round(v / 4) for v in xy]
    if not (radius <= x < W - radius and radius <= y < H - radius):
        return np.empty((0, 0, 3), dtype=np.uint8)
    return frame[y-radius:y+radius+1, x-radius:x+radius+1].astype(np.int16)


def check_action(block, frames):
    press = block["events"]["causal_press"]
    sx, sy, sz = block["set"]["signal_m"]
    onset = []
    switch = block["set"]["switch_m"]
    for shot in block["shots"]:
        start, end = shot["edit"]
        camera = shot["camera_binding"]
        pixel = project_point((sx, sy-.15, sz), camera, block["pixels"])
        for frame in range(start, end):
            patch = rgb_window(frames[frame], pixel)
            demand(patch.size > 0, "signal_out_of_frame", "keep the relay face visible in each view")
            green = (patch[:, :, 1] > 170) & (patch[:, :, 1] - patch[:, :, 0] > 22)
            if int(green.sum()) >= 5:
                onset.append(frame)
        if start < press < end:
            point = project_point(switch, camera, block["pixels"])
            before, after = (rgb_window(frames[n], point, radius=4) for n in (press-1, press))
            demand(before.size > 0 and float(np.abs(after - before).mean()) > 6, "switch_not_visible",
                   "show the cap pressing at the same moment as the relay response")
    demand(onset == list(range(press, len(frames))), "signal_timing",
           "keep the relay dark before contact and lit through the last cut frame")
    return onset[0]


def wav(path, rate, count):
    with wave.open(str(path), "rb") as source:
        demand(source.getnchannels() == 1 and source.getframerate() == rate and source.getsampwidth() == 2
               and source.getnframes() == count, "invalid_mix_clock", "restore frame-aligned mono PCM media")
        return np.frombuffer(source.readframes(count), dtype="<i2").astype(float) / 32768


def verify_audio(edit, root, movie, block, sheet):
    rate, fps = sheet["sample_rate"], edit["fps"]
    stride = rate // fps
    count = stride * edit["total_edit_frames"]
    mix = wav(media_path(root, edit["mix"], "stale_audio"), rate, count)
    stems = np.zeros_like(mix)
    onsets = {}
    for role, reference in edit["stems"].items():
        stem = wav(media_path(root, reference, "stale_audio"), rate, count)
        frames = stem.reshape(-1, stride)
        audible = np.flatnonzero(np.max(np.abs(frames), axis=1) > .008)
        first = sheet["dialogue"]["start"] if role == "dialogue" else sheet["effects"][role]["start"]
        demand(audible.size > 0 and int(audible[0]) == first,
               "stem_onset", "rebuild the cues and stems on the current frame clock")
        onsets[role] = int(audible[0])
        stems += stem
    demand(float(np.max(np.abs(mix - stems))) < .00011, "stale_mix", "remix from the pinned stems")
    pcm = ffmpeg("-xerror", "-i", movie, "-map", "0:a:0", "-ac", "1", "-ar", rate, "-f", "s16le", "pipe:1")
    decoded = np.frombuffer(pcm, dtype="<i2").astype(float) / 32768
    demand(count <= len(decoded) <= count + 1023, "wrong_audio_duration",
           "mux continuous audio across the entire picture edit")
    rms = np.sqrt(np.mean((decoded[:count] - mix).reshape(-1, stride)**2, axis=1))
    press = block["events"]["causal_press"]
    demand(float(rms[:press].max()) < .033, "early_audio",
           "reconform the output mux with the causal sound at or after contact")
    demand(float(rms.max()) < .033, "audio_mismatch", "mux the current PCM mix into the master")
    return {"sample_rate": rate, "edit_samples": count, "decoded_samples": len(decoded),
            "max_frame_pcm_error_rms": round(float(rms.max()), 6), "stem_onset_frames": onsets}


def inspect(root, project_root, dialogue=False):
    docs, pins = project_snapshot(project_root)
    block = resolve(docs)
    sheet = cues(docs, block, dialogue)
    edit = load(root / "timeline.json")
    sound = load(root / "sound.json")
    receipt = load(root / "receipt.json")
    movie = root / "master.mp4"
    demand(receipt["timeline_sha256"] == digest(root / "timeline.json")
           and receipt["conform"]["sha256"] == digest(movie)
           and receipt["project"] == pins
           and receipt["dialogue"] is dialogue, "stale_delivery", "rebuild the delivery against the revised inputs")
    demand(edit == timeline(block, [{"path": r["media"]["path"], "sha256": r["media"]["sha256"]}
                                    for r in edit["shots"]], sound), "wrong_timeline",
           "restore the resolved shot, cast and editorial bindings")
    boards = load(root / "boards.json")
    demand(len(boards["frames"]) == len(block["shots"]), "stale_boards", "render a board for every shot")
    for board, shot in zip(boards["frames"], block["shots"], strict=True):
        media_path(root, board, "stale_boards")
        demand(board["shot"] == shot["id"] and board["edit_frame"] == shot["board_frame"]
               and board["camera"] == shot["camera"] and board["light"] == shot["light"],
               "stale_boards", "render the approved board frame with its camera and light")
    tracks = probe(movie)["streams"]
    demand([s["codec_type"] for s in tracks] == ["video", "audio", "subtitle"], "missing_stream",
           "mux picture, mono sound and selectable English captions")
    video, audio, captions = tracks
    demand(video["codec_name"] == "h264" and audio["codec_name"] == "aac" and captions["codec_name"] == "mov_text"
           and [video["width"], video["height"]] == edit["pixels"]
           and video["avg_frame_rate"] == f'{edit["fps"]}/1'
           and int(audio["sample_rate"]) == sheet["sample_rate"] and audio["channels"] == 1
           and captions["tags"]["language"] == "eng", "wrong_stream_contract",
           "restore the picture, audio and English-caption encoding for the editorial fixture")
    expected_caption_text = subtitle_text(sheet["captions"], edit["fps"]).strip()
    decoded = ffmpeg("-xerror", "-i", movie, "-map", "0:s:0", "-f", "srt", "pipe:1").decode("utf-8").strip()
    subtitle = media_path(root, edit["subtitle"], "stale_subtitle")
    demand(sheet["captions"] == edit["captions"] and decoded == expected_caption_text
           and subtitle.read_text(encoding="utf-8").strip() == expected_caption_text,
           "stale_subtitle", "rebuild and mux the current cue-sheet captions")
    demand(set(edit["stems"]) == set(sheet["effects"]) | ({"dialogue"} if dialogue else set()),
           "wrong_stems", "rebuild the enabled editorial audio roles")
    frames, picture = verify_picture(edit, root, movie)
    picture["relay_first_lit_frame"] = check_action(block, frames)
    audio_check = verify_audio(edit, root, movie, block, sheet)
    return {"status": "VERIFIED", "movie_sha256": digest(movie), "picture": picture, "audio": audio_check,
            "dialogue": dialogue, "actor_ids": list(block["cast"]), "pixels": edit["pixels"], "fps": edit["fps"],
            "caption_frames": [[r["start"], r["end"]] for r in sheet["captions"]]}
