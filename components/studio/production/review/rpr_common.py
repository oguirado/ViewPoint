# SPDX-License-Identifier: AGPL-3.0-only
"""Shared primitives of the pinned review runner (rp4.review-protocol.v1).

Every function here is deterministic for the same input bytes, except the ones
that read the clock or draw random tokens, and those never write into a manifest.
"""
import hashlib
import json
import os
import re
import struct
import subprocess
import zlib
from fractions import Fraction
from pathlib import Path

VERSION = "rp4.review-protocol.v1"
ROOT = Path(__file__).resolve().parent
PROTOCOL_DIR = ROOT / "protocol"
PROTOCOL_FILE = PROTOCOL_DIR / "PROTOCOL.json"
AXES = (
    "textures_materials",
    "silhouette_readability",
    "animation_readability",
    "contact_and_staging",
    "technical_artefacts",
)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
# Children inherit a pinned locale and clock, and never see a display.
ENV = {key: value for key, value in os.environ.items()
       if key not in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")}
ENV.update({"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC"})


class Refused(Exception):
    """The protocol observed what it refuses. Process exit 1."""

    def __init__(self, reason, retry_when, next_command):
        super().__init__(reason)
        self.reason = reason
        self.retry_when = retry_when
        self.next_command = [str(part) for part in next_command]


class Harness(Exception):
    """The check could not run as specified. Process exit 2."""


def canonical(data):
    return json.dumps(data, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def json_bytes(data):
    return canonical(data).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"invalid JSON number {value}")


def parse_json(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def read_json(path):
    try:
        return parse_json(Path(path).read_bytes())
    except FileNotFoundError as exc:
        raise Harness(f"missing file: {path}") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise Harness(f"unreadable JSON in {path}: {exc}") from exc


def write_bytes(path, data, readonly=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.pending")
    pending.write_bytes(data)
    if readonly:
        os.chmod(pending, 0o444)
    pending.replace(path)


def write_json(path, data, readonly=False):
    write_bytes(path, json_bytes(data), readonly)


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sha_text(text):
    return sha_bytes(text.encode("utf-8"))


def frame_token(movie_sha, index, pts, png_sha):
    seed = f"{VERSION}|frame|{movie_sha}|{index}|{pts}|{png_sha}"
    return sha_text(seed)[:16]


def load_protocol():
    protocol = read_json(PROTOCOL_FILE)
    if protocol.get("version") != VERSION or tuple(protocol.get("axes", ())) != AXES:
        raise Harness("protocol version or axes disagree with the runner")
    required = {"BLIND_SOURCE.md", "DISCLOSED_TERMS.json", "RUBRIC.md"}
    if set(protocol["pins"]) != required:
        raise Harness("protocol must pin each auxiliary file")
    for name, pin in protocol["pins"].items():
        if sha_file(PROTOCOL_DIR / name) != pin:
            raise Harness(f"protocol pin mismatch: {name}")
    return protocol


# ---------------------------------------------------------------- PNG codec
def png_encode(rgb):
    """Deterministic PNG: per-row filter chosen by minimum sum of absolute values."""
    import numpy as np

    height, width, _ = rgb.shape
    raw = np.ascontiguousarray(rgb, dtype=np.uint8).reshape(height, width * 3)
    x = raw.astype(np.int16)
    left = np.zeros_like(x)
    left[:, 3:] = x[:, :-3]
    up = np.zeros_like(x)
    up[1:] = x[:-1]
    candidates = (x, x - left, x - up, x - ((left + up) >> 1))
    packed = [(candidate & 0xFF).astype(np.uint8) for candidate in candidates]
    cost = np.stack([np.abs(candidate).sum(axis=1, dtype=np.int64) for candidate in candidates])
    choice = np.argmin(cost, axis=0)
    rows = b"".join(bytes((int(kind),)) + packed[int(kind)][y].tobytes()
                    for y, kind in enumerate(choice))
    deflater = zlib.compressobj(6, zlib.DEFLATED, 15, 8, zlib.Z_DEFAULT_STRATEGY)
    idat = deflater.compress(rows) + deflater.flush()

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def png_rgb_sha(path):
    """Decode a PNG with an independent decoder and hash its RGB24 bytes."""
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        if image.mode != "RGB":
            raise ValueError(f"{path}: PNG is {image.mode}, expected RGB")
        rgb = np.asarray(image)
    return rgb.shape[1], rgb.shape[0], sha_bytes(np.ascontiguousarray(rgb).tobytes())


# ---------------------------------------------------------------- ffmpeg
def _run(command):
    try:
        done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
    except OSError as exc:
        raise Harness(f"cannot run {command[0]}: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()[-600:]
        if command[0] in ("ffmpeg", "ffprobe"):
            raise Refused("movie_undecodable", "supply a movie that decodes to its last frame", command)
        raise Harness(f"{command[0]} exited {done.returncode}: {detail}")
    return done.stdout


def ffmpeg_base(video):
    return ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
            "-threads", "1", "-filter_threads", "1", "-copyts", "-protocol_whitelist", "file,pipe",
            "-f", "mov", "-i", str(video)]


def decoded_catalog(video):
    """Decode every frame to EOF and return its presentation-time catalog."""
    video = Path(video)
    probe = json.loads(_run([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-f", "mov", "-select_streams", "V",
        "-show_entries", "stream=index,time_base,nb_frames,width,height,avg_frame_rate", "-of", "json", str(video)]))
    streams = probe.get("streams", [])
    if len(streams) != 1:
        raise Refused("movie_streams", "supply a movie with exactly one video stream", ["rp_review.py"])
    stream = streams[0]
    try:
        clock = Fraction(stream["time_base"])
    except (KeyError, ZeroDivisionError, ValueError) as exc:
        raise Harness(f"unreadable video time base: {exc}") from exc
    if clock <= 0:
        raise Harness("nonpositive video time base")
    raw_options = [
        "-map", f"0:{stream['index']}", "-an", "-sn", "-dn", "-fps_mode", "passthrough",
        "-c:v", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1",
        "-enc_time_base", f"{clock.numerator}:{clock.denominator}", "-avoid_negative_ts", "disabled",
    ]
    output = _run(ffmpeg_base(video) + raw_options + ["-f", "framehash", "-hash", "sha256", "pipe:1"]).decode("ascii")
    headers = dict(line[1:].split(": ", 1) for line in output.splitlines()
                   if line.startswith("#") and ": " in line)
    if headers.get("hash") != "SHA256" or Fraction(headers["tb 0"]) != clock:
        raise Harness("unexpected decoded framehash format or clock")
    if headers.get("sar 0") not in {"1/1", "0/1"}:
        raise Refused("movie_aspect_ratio", "supply a movie with square pixels", ["rp_review.py"])
    width, height = map(int, headers["dimensions 0"].split("x"))
    frames = []
    for line in output.splitlines():
        if line.startswith("#"):
            continue
        number, dts, pts, duration, size, sha = map(str.strip, line.split(","))
        pts = int(pts)
        if (number != "0" or int(dts) != pts or int(size) != width * height * 3
                or not SHA_RE.match(sha) or (frames and pts <= frames[-1]["pts"])):
            raise Harness("invalid or nonmonotonic decoded frame record")
        frames.append({"index": len(frames), "pts": pts, "duration_ticks": int(duration), "rgb24_sha256": sha})
    advertised = stream.get("nb_frames", "N/A")
    if not frames or (advertised != "N/A" and int(advertised) != len(frames)):
        raise Refused("movie_frame_count", "supply a movie whose container count equals its decoded frames",
                      ["rp_review.py"])
    catalog = {
        "time_base": str(clock), "width": width, "height": height, "frame_count": len(frames),
        "avg_frame_rate": stream.get("avg_frame_rate"), "frames": frames,
    }
    return catalog, raw_options


def select_expression(indices):
    ranges = []
    for index in indices:
        if ranges and index == ranges[-1][1] + 1:
            ranges[-1][1] = index
        else:
            ranges.append([index, index])
    return "+".join(f"eq(n,{a})" if a == b else f"between(n,{a},{b})" for a, b in ranges)


def time_seconds(pts, time_base):
    """Presentation time in seconds: ticks times the clock, as a fixed six-place decimal."""
    return f"{float(Fraction(pts) * Fraction(time_base)):.6f}"
