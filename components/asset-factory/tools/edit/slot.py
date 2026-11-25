#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Render and verify a signed frame-ordinal picture edit."""
import argparse
from contextlib import ExitStack, closing
from fractions import Fraction
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


EDIT_SCHEMA = "af.edit/v1"
LEGACY_SCHEMA = "rp3.edit/v1"
RUNTIME_ENV = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC"}

FFMPEG = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-xerror"]
FFPROBE = ["ffprobe", "-v", "error"]
CLIP_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,nut,avi"
CLIP_SUFFIXES = {".mov", ".mp4", ".mkv", ".webm", ".nut", ".avi"}
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


class EditError(Exception):
    pass


def require(condition, cause, detail):
    if not condition:
        raise EditError(f"{cause}: {detail}")


def digest(path):
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


def object_pairs(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), "INVALID_JOB", "duplicate member")
    return result


def checked_output(args, input_bytes=None, pass_fds=()):
    result = subprocess.run(args, input=input_bytes, capture_output=True, pass_fds=pass_fds)
    require(
        result.returncode == 0 and not result.stderr,
        "TOOL_FAILURE",
        f"{args[0]} rc={result.returncode}: {result.stderr.decode(errors='replace').strip()}",
    )
    return result.stdout


def verify_signature(raw, signature, public_key):
    require(len(signature) == 64, "INVALID_SIGNATURE", "expected a raw Ed25519 signature")
    key = checked_output(
        ["openssl", "pkey", "-pubin", "-inform", "PEM", "-outform", "DER"], public_key
    )
    require(
        key.startswith(ED25519_SPKI_PREFIX) and len(key) == len(ED25519_SPKI_PREFIX) + 32,
        "INVALID_SIGNATURE",
        "expected an Ed25519 public key",
    )
    # OpenSSL's Ed25519 verifier needs seekable input. Verify the exact bytes parsed below.
    with ExitStack() as stack:
        files = []
        for name, data in [("job", raw), ("signature", signature), ("public-key", key)]:
            descriptor = os.memfd_create(name)
            handle = stack.enter_context(os.fdopen(descriptor, "w+b"))
            handle.write(data)
            handle.seek(0)
            files.append(descriptor)
        job_fd, sig_fd, pub_fd = files
        result = subprocess.run(
            ["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-keyform", "DER",
             "-inkey", f"/proc/self/fd/{pub_fd}", "-in", f"/proc/self/fd/{job_fd}",
             "-sigfile", f"/proc/self/fd/{sig_fd}"],
            pass_fds=tuple(files), capture_output=True,
        )
    require(result.returncode == 0, "INVALID_SIGNATURE", "job bytes fail Ed25519 verification")
    return sha256(key).hexdigest()


def fields(value, required, optional=()):
    require(isinstance(value, dict), "INVALID_JOB", "expected an object")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    require(not missing and not unknown, "INVALID_JOB",
            f"missing fields {sorted(missing)}, unknown fields {sorted(unknown)}")


def integer(value, name, minimum=0, maximum=2**31 - 1):
    require(type(value) is int and minimum <= value <= maximum, "INVALID_JOB",
            f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def array(value, name):
    require(isinstance(value, list), "INVALID_JOB", f"{name} must be an array")
    return value


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value),
            "INVALID_JOB", f"invalid identifier {value!r}")
    return value


def input_options(asset):
    if asset["kind"] == "png":
        return ["-protocol_whitelist", "file", "-f", "png_pipe"]
    return ["-protocol_whitelist", "file", "-format_whitelist", CLIP_FORMATS]


def check_asset_pin(asset):
    require(digest(asset["resolved"]) == asset["sha256"], "ASSET_DIGEST",
            asset["path"])


def validate_clip_clock(name, rate, time_base, frames):
    clock = Fraction(time_base or "0")
    ticks = 1 / (rate * clock) if clock > 0 else None
    origin = frames[0].get("pts")
    require(ticks is not None and ticks.denominator == 1 and type(origin) is int,
            "ASSET_CFR", f"{name} requires exact project CFR ({rate} fps)")
    ticks = ticks.numerator
    for index, frame in enumerate(frames):
        duration = frame.get("duration", frame.get("pkt_duration"))
        require(frame.get("pts") == origin + index * ticks and duration == ticks,
                "ASSET_CFR", f"{name} frame {index} does not match project CFR ({rate} fps)")


def load_asset(asset, root, rate):
    fields(asset, ["path", "kind", "sha256"], ["conform"])
    name = asset["path"]
    require(isinstance(name, str) and name and "\x00" not in name, "INVALID_JOB",
            "asset path must be a nonempty relative path")
    relative = Path(name)
    require(not relative.is_absolute() and ".." not in relative.parts, "INVALID_JOB",
            f"invalid project-relative asset path {name!r}")
    kind = asset["kind"]
    suffix = relative.suffix.lower()
    require((kind == "png" and suffix == ".png") or (kind == "clip" and suffix in CLIP_SUFFIXES),
            "INVALID_JOB", f"unsupported asset kind or extension for {name!r}")
    require("conform" not in asset or
            (kind == "clip" and asset["conform"] in ("project_cfr", "ordinal")),
            "INVALID_JOB", f"invalid clip conform mode for {name!r}")
    check_clock = kind == "clip" and asset.get("conform") != "ordinal"
    pin = asset["sha256"]
    require(isinstance(pin, str) and re.fullmatch(r"[a-f0-9]{64}", pin), "INVALID_JOB",
            f"invalid SHA-256 for {name!r}")
    resolved = (root / relative).resolve(strict=True)
    require(resolved.is_relative_to(root) and resolved.is_file(), "ASSET_PATH", name)
    asset = {**asset, "resolved": resolved}
    check_asset_pin(asset)
    entries = "stream=width,height,time_base:stream_disposition=attached_pic:frame=width,height"
    if check_clock:
        entries += ",pts,duration,pkt_duration"
    result = json.loads(checked_output(
        FFPROBE + input_options(asset) + [
            "-select_streams", "v", "-show_entries", entries,
            "-of", "json", str(resolved),
        ]
    ))
    streams = result.get("streams", [])
    require(len(streams) == 1 and not streams[0].get("disposition", {}).get("attached_pic"),
            "ASSET_VIDEO", f"{name} must have one picture stream")
    stream = streams[0]
    frames = result.get("frames", [])
    count = len(frames)
    require(count > 0 and (kind != "png" or count == 1), "ASSET_VIDEO",
            f"{name} has invalid decoded frame count")
    require(all(frame.get("width") == stream["width"] and frame.get("height") == stream["height"]
                for frame in frames), "ASSET_GEOMETRY", f"{name} changes frame dimensions")
    if check_clock:
        validate_clip_clock(name, rate, stream.get("time_base"), frames)
    return {**asset, "count": count, "width": stream["width"], "height": stream["height"]}


def load_job(args):
    raw = args.job.read_bytes()
    signer = verify_signature(raw, args.signature.read_bytes(), args.public_key.read_bytes())
    job = json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs)
    fields(job, ["schema", "fps", "width", "height", "assets", "shots"])
    require(job["schema"] in (EDIT_SCHEMA, LEGACY_SCHEMA), "INVALID_JOB", "unsupported schema")
    fps = job["fps"]
    require(isinstance(fps, str) and re.fullmatch(r"[1-9][0-9]{0,9}/[1-9][0-9]{0,9}", fps),
            "INVALID_JOB", "fps must be a positive reduced rational string")
    rate = Fraction(fps)
    require(fps == f"{rate.numerator}/{rate.denominator}", "INVALID_JOB", "fps must be reduced")
    integer(rate.numerator, "fps numerator", 1)
    integer(rate.denominator, "fps denominator", 1)
    width = integer(job["width"], "width", 1)
    height = integer(job["height"], "height", 1)
    require(isinstance(job["assets"], dict) and job["assets"], "INVALID_JOB", "missing assets")
    root = args.asset_root.resolve(strict=True)
    require(root.is_dir(), "ASSET_PATH", "asset root must be a directory")
    assets = {identifier(key): load_asset(value, root, rate) for key, value in job["assets"].items()}
    shots = array(job["shots"], "shots")
    require(bool(shots), "INVALID_JOB", "missing shots")
    seen = set()
    for shot in shots:
        fields(shot, ["id", "asset", "start_frame", "end_frame"], ["layers", "holds"])
        shot_id = identifier(shot["id"])
        require(shot_id not in seen, "INVALID_JOB", f"duplicate shot {shot_id}")
        seen.add(shot_id)
        base = get_asset(assets, shot["asset"])
        require((base["width"], base["height"]) == (width, height), "ASSET_GEOMETRY",
                f"{shot_id} base must match the picture canvas")
        start = integer(shot["start_frame"], f"{shot_id}.start_frame", 0, base["count"] - 1)
        max_end = base["count"] if base["kind"] == "clip" else 2**31 - 1
        end = integer(shot["end_frame"], f"{shot_id}.end_frame", start + 1, max_end)
        length = end - start
        for layer in array(shot.get("layers", []), f"{shot_id}.layers"):
            fields(layer, ["asset", "start_frame", "at_frame", "frames", "x", "y"])
            asset = get_asset(assets, layer["asset"])
            at = integer(layer["at_frame"], f"{shot_id}.layer.at_frame", 0, length - 1)
            count = integer(layer["frames"], f"{shot_id}.layer.frames", 1, length - at)
            max_start = asset["count"] - count if asset["kind"] == "clip" else 0
            integer(layer["start_frame"], f"{shot_id}.layer.start_frame", 0, max_start)
            integer(layer["x"], f"{shot_id}.layer.x", 0, width - asset["width"])
            integer(layer["y"], f"{shot_id}.layer.y", 0, height - asset["height"])
        holds = set()
        for hold in array(shot.get("holds", []), f"{shot_id}.holds"):
            fields(hold, ["after_frame", "extra_frames"])
            after = integer(hold["after_frame"], f"{shot_id}.hold.after_frame", 0, length - 1)
            require(after not in holds, "INVALID_JOB", f"{shot_id} repeats a hold position")
            holds.add(after)
            integer(hold["extra_frames"], f"{shot_id}.hold.extra_frames", 1)
    return job, rate, assets, sha256(raw).hexdigest(), signer


def get_asset(assets, key):
    key = identifier(key)
    require(key in assets, "INVALID_JOB", f"unknown asset {key}")
    return assets[key]


def picture_graph(shot, rate, assets):
    base = assets[shot["asset"]]
    start, end = shot["start_frame"], shot["end_frame"]
    clock = f"settb=expr={rate.denominator}/{rate.numerator}"
    inputs = input_options(base) + ["-noautorotate", "-i", str(base["resolved"])]
    trim_end, loop = end, ""
    if base["kind"] == "png":
        trim_end, loop = 1, f",loop=loop={end - 1}:size=1:start=0"
    graph = [f"[0:v:0]trim=start_frame={start}:end_frame={trim_end},"
             f"format=rgb24{loop},setsar=1,{clock},setpts=N[base]"]
    current = "base"
    for index, layer in enumerate(shot.get("layers", []), 1):
        asset = assets[layer["asset"]]
        inputs += input_options(asset) + ["-noautorotate", "-i", str(asset["resolved"])]
        start, duration, at = layer["start_frame"], layer["frames"], layer["at_frame"]
        trim_end = start + duration if asset["kind"] == "clip" else 1
        loop = f",loop=loop={duration - 1}:size=1:start=0" if asset["kind"] == "png" else ""
        graph.append(f"[{index}:v:0]trim=start_frame={start}:end_frame={trim_end},"
                     f"format=rgba{loop},{clock},setpts=N+{at}[layer{index}]")
        graph.append(f"[{current}][layer{index}]overlay=x={layer['x']}:y={layer['y']}:"
                     f"format=rgb:alpha=straight:eof_action=pass:repeatlast=0[mix{index}]")
        current = f"mix{index}"
    return inputs, ";".join(graph), current


def edit_frames(job, rate, assets):
    """Yield one composited RGB frame and its provenance at a time."""
    byte_count = job["width"] * job["height"] * 3
    sequence = 0
    previous_hash = None
    include_shot_frame = job["schema"] == EDIT_SCHEMA
    for shot in job["shots"]:
        inputs, graph, label = picture_graph(shot, rate, assets)
        base_is_still = assets[shot["asset"]]["kind"] == "png"
        layers = shot.get("layers", [])
        holds = {hold["after_frame"]: hold["extra_frames"] for hold in shot.get("holds", [])}
        args = FFMPEG + [
            "-filter_complex_threads", "1", *inputs, "-filter_complex", graph,
            "-map", f"[{label}]", "-fps_mode", "passthrough",
            "-c:v", "rawvideo", "-threads:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
        ]
        with subprocess.Popen(args, stdout=subprocess.PIPE) as reader:
            try:
                for local, source_frame in enumerate(range(shot["start_frame"], shot["end_frame"])):
                    pixels = reader.stdout.read(byte_count)
                    require(len(pixels) == byte_count, "SOURCE_FRAME_COUNT", shot["id"])
                    picture_hash = sha256(pixels).hexdigest()
                    active_layers = []
                    for order, layer in enumerate(layers):
                        offset = local - layer["at_frame"]
                        if 0 <= offset < layer["frames"]:
                            layer_frame = layer["start_frame"]
                            if assets[layer["asset"]]["kind"] == "clip":
                                layer_frame += offset
                            active_layers.append({"order": order, "asset": layer["asset"],
                                                  "source_frame": layer_frame})
                    entry = {"shot": shot["id"], "asset": shot["asset"],
                             "source_frame": 0 if base_is_still else source_frame,
                             "layers": active_layers,
                             "rgb24_sha256": picture_hash}
                    if include_shot_frame:
                        entry["shot_frame"] = local
                    origin = sequence
                    for repeat in range(1 + holds.get(local, 0)):
                        row = {"frame": sequence, "pts": sequence * rate.denominator,
                               "hold_of": origin if repeat else None, **entry}
                        if not repeat and picture_hash == previous_hash:
                            row["source_repeat"] = True
                        yield pixels, row
                        sequence += 1
                    previous_hash = picture_hash
                require(not reader.stdout.read(1), "SOURCE_FRAME_COUNT", shot["id"])
                require(reader.wait() == 0, "TOOL_FAILURE", f"FFmpeg decode of {shot['id']}")
            finally:
                if reader.poll() is None:
                    reader.kill()


def decode_master(master, job, rate, expected):
    probe = json.loads(checked_output(FFPROBE + [
        "-protocol_whitelist", "file", "-f", "mov", "-show_entries",
        "stream=codec_type,codec_name,pix_fmt,width,height,time_base,duration_ts:stream_side_data=rotation",
        "-of", "json", str(master),
    ]))
    video = {"codec_type": "video", "codec_name": "png", "pix_fmt": "rgb24",
             "width": job["width"], "height": job["height"],
             "time_base": f"1/{rate.numerator}"}
    streams = probe.get("streams", [])
    require(len(streams) == 1, "MASTER_STREAM", "expected one MOV picture stream")
    duration = streams[0].pop("duration_ts", None)
    require(streams[0] == video, "MASTER_STREAM", "unexpected MOV picture stream format")
    result = checked_output(FFMPEG + [
        "-copyts", "-protocol_whitelist", "file", "-f", "mov", "-noautorotate", "-i", str(master),
        "-map", "0:v:0", "-fps_mode", "passthrough", "-enc_time_base", "demux",
        "-c:v", "rawvideo", "-pix_fmt", "rgb24", "-threads:v", "1",
        "-f", "framehash", "-hash", "sha256", "pipe:1",
    ]).decode("ascii")
    ticks = [line.split(":", 1)[1].strip() for line in result.splitlines()
             if line.startswith("#tb 0:")]
    require(ticks == [video["time_base"]], "MASTER_PTS", f"decoded time base {ticks}")
    rows = [line.split(",") for line in result.splitlines() if line and not line.startswith("#")]
    require(len(rows) == len(expected), "MASTER_FRAME_COUNT",
            f"decoded {len(rows)}, expected {len(expected)}")
    previous_hash = None
    for row, entry in zip(rows, expected, strict=True):
        index = entry["frame"]
        require(int(row[2]) == entry["pts"], "MASTER_PTS",
                f"frame {index} has PTS {row[2].strip()}, expected {entry['pts']}")
        require(int(row[3]) == rate.denominator, "MASTER_PTS",
                f"frame {index} has duration {row[3].strip()}, expected {rate.denominator}")
        frame_hash = row[-1].strip()
        if frame_hash != entry["rgb24_sha256"] and frame_hash == previous_hash:
            raise EditError(f"UNDECLARED_REPEAT: frame {index}")
        require(frame_hash == entry["rgb24_sha256"], "FRAME_IDENTITY",
                f"frame {index} does not match {entry['shot']}:{entry['source_frame']}")
        previous_hash = frame_hash
    require(duration == len(expected) * rate.denominator, "MASTER_PTS",
            f"unexpected MOV track duration of {duration} ticks")


def proof_map(master, job_schema, rate, assets, job_hash, signer, frames):
    for asset in assets.values():
        check_asset_pin(asset)
    return {
        "schema": ("af.edit.frame-map/v1" if job_schema == EDIT_SCHEMA
                   else "rp3.edit.frame-map/v1"),
        "job_sha256": job_hash,
        "signer_spki_sha256": signer,
        "master_sha256": digest(master),
        "fps": f"{rate.numerator}/{rate.denominator}",
        "time_base": f"1/{rate.numerator}",
        "frame_count": len(frames),
        "assets": {key: {"path": value["path"], "sha256": value["sha256"]}
                   for key, value in assets.items()},
        "frames": frames,
    }


def encode_master(master, job, rate, assets):
    args = FFMPEG + [
        "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{job['width']}x{job['height']}",
        "-framerate", str(job["fps"]), "-i", "pipe:0", "-map", "0:v:0",
        "-fps_mode", "passthrough", "-c:v", "png", "-pix_fmt", "rgb24", "-threads:v", "2",
        "-video_track_timescale", str(rate.numerator), "-movie_timescale", str(rate.numerator),
        "-f", "mov", str(master),
    ]
    frames = []
    with subprocess.Popen(args, stdin=subprocess.PIPE) as encoder:
        try:
            with closing(edit_frames(job, rate, assets)) as pictures:
                for pixels, entry in pictures:
                    encoder.stdin.write(pixels)
                    frames.append(entry)
            encoder.stdin.close()
            require(encoder.wait() == 0, "TOOL_FAILURE", "FFmpeg master encode")
        finally:
            if encoder.poll() is None:
                encoder.kill()
    return frames


def render(args, job, rate, assets, job_hash, signer):
    output = args.output.absolute()
    require(not os.path.lexists(output), "OUTPUT_EXISTS", str(output))
    with tempfile.TemporaryDirectory(prefix=".edit-", dir=output.parent) as stage:
        candidate = Path(stage) / "picture"
        candidate.mkdir()
        master = candidate / "master.mov"
        frames = encode_master(master, job, rate, assets)
        decode_master(master, job, rate, frames)
        proof = proof_map(master, job["schema"], rate, assets, job_hash, signer, frames)
        (candidate / "frame-map.json").write_text(
            json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        candidate.rename(output)
    return len(frames)


def verify(args, job, rate, assets, job_hash, signer):
    master = args.output / "master.mov"
    frames = [entry for _, entry in edit_frames(job, rate, assets)]
    decode_master(master, job, rate, frames)
    proof = proof_map(master, job["schema"], rate, assets, job_hash, signer, frames)
    observed = json.loads((args.output / "frame-map.json").read_text(encoding="utf-8"),
                          object_pairs_hook=object_pairs)
    require(json.dumps(observed, sort_keys=True) == json.dumps(proof, sort_keys=True),
            "FRAME_MAP", "map differs from signed job and verified master")
    return len(frames)


def main():
    os.environ.update(RUNTIME_ENV)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["render", "verify"])
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        job = load_job(args)
        operation = render if args.mode == "render" else verify
        count = operation(args, *job)
    except (EditError, OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"PASS {args.mode}: {count} decoded frames")
    return 0


if __name__ == "__main__":
    sys.exit(main())
