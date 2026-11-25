"""Independently read the native edit, caption, picture and audio evidence."""
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import wave

from PIL import Image, ImageChops

ENV = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC"}
GUARDS = {
    "CPU": 0, "CUDA": {"rc": 3, "reason": "cuda_admission_unavailable"},
    "sequencer_active": {"rc": 3, "reason": "scene_sequencer_render_unqualified"},
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def pin(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def ffmpeg(*args):
    return subprocess.check_output(
        ["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-xerror", *args], env=ENV
    )


def streams(path):
    return json.loads(subprocess.check_output(
        ["/usr/bin/ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", path],
        env=ENV
    ))["streams"]


def verify_edit(job, board, shots, edit):
    starts = {scene["id"]: scene["start_frame"] for scene in board["scenes"]}
    expected = [(cut["scene"], starts[cut["scene"]] + frame)
                for cut in job["edit"] for frame in range(cut["in"], cut["out"])]
    rows = edit["frames"]
    by_id = {shot["id"]: shot for shot in shots}
    require(len(rows) == len(expected) == edit["frame_count"], "edit frame count")
    for index, (row, (scene, frame)) in enumerate(zip(rows, expected)):
        shot = by_id[row["shot"]]
        ordinal = frame - shot["start_frame"]
        require(shot["scene_id"] == scene and 0 <= ordinal < shot["end_frame"] - shot["start_frame"]
                and row["frame"] == index and row["source_frame"] == ordinal
                and row["asset"] == f"{shot['id']}-ui", "edit order or trim drift")
        captions = any(cue["kind"] == "caption" and cue["id"] in shot["cue_ids"] for cue in board["cues"])
        layers = [{"order": 0, "asset": f"{shot['id']}-text", "source_frame": ordinal}] if captions else []
        require(row["layers"] == layers, "caption layer timeline drift")
    return [(index, by_id[row["shot"]], frame) for index, (row, (_, frame)) in enumerate(zip(rows, expected))]


def verify_captions(output, film, board, job, frames):
    count = 0
    width, height = (job["profile"][key] for key in ("width", "height"))
    for cue in (cue for cue in board["cues"] if cue["kind"] == "caption"):
        sample = next((row for row in frames if cue["start_frame"] <= row[2] < cue["end_frame"]), None)
        if sample is None:
            continue
        index, shot, source_frame = sample
        shot_dir = output / shot["id"]
        raw = ffmpeg("-i", film, "-map", "0:v:0", "-vf", f"select=eq(n\\,{index})",
                     "-frames:v", "1", "-pix_fmt", "rgb24", "-threads:v", "1", "-f", "rawvideo", "-")
        composed = Image.frombytes("RGB", (width, height), raw)
        with Image.open(shot_dir / f"plane/frames/{source_frame - shot['start_frame']:06}.png") as layer:
            base = layer.convert("RGB")
        x, y, w, h = job["planes"][shot["scene_id"]]["viewport"]
        ui_box = x, y, x + w, y + h
        require(ImageChops.difference(composed.crop(ui_box), base.crop(ui_box)).getbbox() is None,
                "composite changed UI pixels")
        with Image.open(shot_dir / f"text/delivery/plates/{source_frame:06}.png") as text:
            alpha = text.getchannel("A").getbbox()
        require(alpha is not None and alpha[1] >= y + h and alpha[3] <= height * 0.95,
                "caption missing or overlaps protected picture")
        require(ImageChops.difference(composed.crop(alpha), base.crop(alpha)).getbbox() is not None,
                "caption absent from decoded master")
        receipt = read(shot_dir / "text/delivery/receipt.json")
        require(cue["text"] in {item["text"] for item in receipt["editable_text"]},
                "editable caption content drift")
        count += 1
    return count


def verify_unscaled_planes(output, project, board, job, frames):
    """For pixel-aligned plates, verify rendered crops against the authored inputs."""
    surfaces = {row["id"]: row for row in board["surfaces"]}
    scenes = {row["id"]: row for row in board["scenes"]}
    crops = {}
    checks = {"still": 0, "clip": 0}
    for _, shot, frame in frames:
        scene = scenes[shot["scene_id"]]
        plane = job["planes"][scene["id"]]
        x, y, width, height = plane["viewport"]
        sx, sy, sw, sh = plane["crop"]
        if (width, height) != (sw, sh) or plane.get("cursor"):
            continue
        clip = plane.get("frames")
        surface = surfaces[clip[frame - scene["start_frame"]] if clip else scene["surface_ids"][0]]
        source_box = (sx, sy, sx + sw, sy + sh)
        key = (surface["id"], source_box)
        if key not in crops:
            require(pin(project / surface["path"]) == surface["sha256"], "authored surface digest drift")
            with Image.open(project / surface["path"]) as image:
                crops[key] = image.convert("RGB").crop(source_box)
        plate = output / shot["id"] / f"plane/frames/{frame - shot['start_frame']:06}.png"
        with Image.open(plate) as image:
            actual = image.convert("RGB").crop((x, y, x + width, y + height))
        require(ImageChops.difference(actual, crops[key]).getbbox() is None, "authored plate frame drift")
        checks["clip" if clip else "still"] += 1
    return checks


def verify_audio(job_path, output, result, video_frames, fps):
    spec = read(job_path)["audio"]
    master, delivery = (output / "film" / media for media in ("master.mov", "delivery.mp4"))
    master_audio, delivery_audio = ([row for row in streams(path) if row["codec_type"] == "audio"]
                                    for path in (master, delivery))
    if spec["mode"] == "silent":
        require(result["voice_kind"] == "silent" and not master_audio and not delivery_audio,
                "silent disposition produced an audio track")
        disposition = job_path.parent / spec["silence_disposition"]["path"]
        require("sha256:" + pin(disposition) == spec["silence_disposition"]["sha256"],
                "silence disposition digest")
        return {"mode": "silent", "sample_count": None, "pcm_sha256": None, "verified_cues": None,
                "master_audio_streams": len(master_audio), "delivery_audio_streams": len(delivery_audio),
                "silence_disposition_sha256": pin(disposition)}
    narration = spec["narration"]
    timing = read(job_path.parent / narration["timing_path"])
    pcm = ffmpeg("-i", master, "-map", "0:a:0", "-c:a", "pcm_s16le", "-f", "s16le", "-")
    rate, channels = timing["sample_rate"], timing["channels"]
    samples = -(-(video_frames * rate * fps.denominator) // fps.numerator)
    pcm_pin = "sha256:" + hashlib.sha256(pcm).hexdigest()
    require(pcm_pin == timing["pcm_sha256"], "lossless PCM identity")
    require(len(pcm) == samples * channels * 2 and samples == timing["sample_count"], "PCM sample window")
    wav = job_path.parent / narration["wav_path"]
    require("sha256:" + pin(wav) == narration["wav_sha256"], "authored WAV digest")
    with wave.open(str(wav), "rb") as source:
        require((source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getnframes())
                == (rate, channels, 2, samples), "authored WAV format")
        require(pcm == source.readframes(samples), "lossless PCM differs from authored WAV")
    require(len(master_audio) == len(delivery_audio) == 1, "missing narrated audio track")
    audio = delivery_audio[0]
    require((audio["codec_name"], int(audio["sample_rate"]), audio["channels"], int(audio["start_pts"]),
             int(audio["duration_ts"]) * Fraction(audio["time_base"])) ==
            ("aac", rate, channels, 0, Fraction(samples, rate)), "delivery audio metadata")
    require(result["voice_kind"] == narration["source_kind"]
            and result["metrics"]["delivery_pcm_correlation_ppm"] >= 950000, "delivery AAC integrity")
    return {"mode": "narrated", "sample_count": samples, "pcm_sha256": pcm_pin,
            "verified_cues": len(timing["cues"]), "authored_wav_sha256": pin(wav),
            "timing_sha256": pin(job_path.parent / narration["timing_path"]),
            "master_audio_streams": len(master_audio), "delivery_audio_streams": len(delivery_audio),
            "sample_rate": rate, "channels": channels}


def verify(output, job_path):
    job = read(job_path)
    board_path = job_path.parent / job["board"]
    board = read(board_path)
    result = read(output / "receipt.json")
    profile = job["profile"]
    fps = Fraction(*profile["fps"])
    require(result["status"] == "PASS" and result["schema"] == "rp4.native-build/v2"
            and result["job_sha256"] == pin(job_path) and result["board_sha256"] == pin(board_path)
            and result["profile"] == profile and result["locale"] == job["locale"], "unbound job result")
    require(set(result["slot_receipts"]) == {"STORY", "TEXT", "PLANE", "EDIT", "AUDIO", "BENCH"},
            "missing slot receipt")
    for unit, artifacts in result["slot_receipts"].items():
        require(unit == "TEXT" or artifacts, f"empty {unit} evidence")
        for artifact in artifacts:
            require(pin(output / artifact["path"]) == artifact["sha256"], f"bad {unit} evidence pin")
    require(result["f5_guards"] == read(output / "f5-guards.json") == GUARDS, "F5 guard results")

    story = read(output / "shots.json")
    edit_path = output / "edit/frame-map.json"
    edit = read(edit_path)
    frames = verify_edit(job, board, story["shots"], edit)
    film = output / "film/master.mov"
    decoded = ffmpeg("-i", film, "-map", "0:v:0", "-pix_fmt", "rgb24", "-threads:v", "1",
                     "-f", "framehash", "-hash", "sha256", "-").decode().splitlines()
    require(f"#tb 0: {1 / fps}" in decoded, "lossless video clock")
    hashes = [line.replace(" ", "").split(",") for line in decoded if not line.startswith("#")]
    readback = [{"pts": int(row[2]), "duration": int(row[3]), "rgb24_sha256": row[-1]} for row in hashes]
    require(readback == [{"pts": i, "duration": 1, "rgb24_sha256": row["rgb24_sha256"]}
                         for i, row in enumerate(edit["frames"])], "lossless frame identity or PTS")
    plane_checks = verify_unscaled_planes(output, job_path.parent, board, job, frames)
    caption_count = verify_captions(output, film, board, job, frames)
    delivery = output / "film/delivery.mp4"
    ffmpeg("-i", delivery, "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-")
    video, = (row for row in streams(delivery) if row["codec_type"] == "video")
    require((video["codec_name"], video["width"], video["height"], Fraction(video["r_frame_rate"]),
             int(video["nb_read_frames"]), int(video["start_pts"])) ==
            ("h264", profile["width"], profile["height"], fps, len(frames), 0), "delivery video metadata")
    audio = verify_audio(job_path, output, result, len(frames), fps)
    delivery_pin = pin(delivery)
    require(read(output / "bench.json") == {
        "schema": "rp4.native-plate-evidence/v1", "profile": profile, "frame_count": len(frames),
        "frame_map_sha256": pin(edit_path), "delivery_sha256": delivery_pin,
    }, "bad local plate evidence")
    mux = read(output / "film/receipt.json")
    return {"canvas": [profile["width"], profile["height"]], "fps": str(fps), "locale": job["locale"],
            "duration_seconds": str(len(frames) / fps), "frame_count": len(frames), "captions": caption_count,
            "pixel_checked_plane_frames": plane_checks,
            "caption_texts": [cue["text"] for cue in board["cues"] if cue["kind"] == "caption"],
            "surface_pins": [surface["sha256"] for surface in board["surfaces"]],
            "edit_scenes": [cut["scene"] for cut in job["edit"]],
            "schedule_sha256": story["pins"]["schedule_sha256"], "edit_sha256": pin(edit_path),
            "decoded_video": {"pixel_format": "rgb24", "time_base": str(1 / fps), "frames": readback},
            "master_sha256": pin(film), "delivery_sha256": delivery_pin,
            "locale_env": {key: ENV[key] for key in ("LC_ALL", "LANG", "TZ")},
            "f5_guards": result["f5_guards"], "source_manifest_sha256": result["source_manifest_sha256"],
            "mux_module_sha256": mux["mux_module_sha256"], "audio": audio,
            "audio_metrics": result["metrics"], "job_sha256": result["job_sha256"],
            "native_receipt_sha256": pin(output / "receipt.json")}


def main():
    baseline, baseline_job, heldout, heldout_job = map(Path, sys.argv[1:])
    first, second = verify(baseline, baseline_job), verify(heldout, heldout_job)
    for field in ("canvas", "fps", "locale", "duration_seconds", "frame_count", "schedule_sha256",
                  "edit_sha256", "caption_texts", "surface_pins", "edit_scenes", "audio", "delivery_sha256"):
        require(first[field] != second[field], f"held-out job did not change {field}")
    require(first["captions"] > 0 and second["captions"] > 0, "missing generic editable captions")
    require(first["audio"]["verified_cues"] >= 2 and second["audio"]["mode"] == "silent",
            "missing alternate audio evidence")
    require(first["pixel_checked_plane_frames"]["still"] == first["frame_count"]
            and sum(second["pixel_checked_plane_frames"].values()) == second["frame_count"]
            and second["pixel_checked_plane_frames"]["clip"] > 0, "missing pixel-aligned plane evidence")
    print(json.dumps({"gate": "positive", "baseline": first, "held_out": second}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
