# SPDX-License-Identifier: AGPL-3.0-only
"""Presentation-order container evidence from two independent FFmpeg outputs."""
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
import json
import os
import re
import stat
import zlib

from .proc import ResourceLimitError, run_child

MAX_INPUT = 1 << 30
MAX_FRAMES = 20000
MAX_FRAME = 8 << 20
MAX_DECODED = 4 << 30
INPUT_OPTIONS = ["-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm",
                 "-threads", "1", "-max_pixels", str(MAX_FRAME // 3)]


@dataclass(frozen=True)
class Track:
    kind: str
    media_type: str
    index: int
    base: Fraction
    width: int
    height: int
    pts: list
    codec: str
    container: str
    clock: str
    duration_ms: float | None
    decoder: str
    source_sha: str
    byte_size: int
    packet_clock: str


def check_extent(w, h, count):
    if count <= 0 or w <= 0 or h <= 0:
        raise ValueError("missing frame geometry or presentation frames")
    if count > MAX_FRAMES:
        raise ResourceLimitError("FRAME_COUNT_LIMIT: frame count exceeds admission limit")
    frame_bytes = w * h * 3
    if max(w, h) > 8192 or frame_bytes > MAX_FRAME:
        raise ResourceLimitError("FRAME_BYTES_LIMIT: RGB frame extent exceeds admission limit")
    if frame_bytes * count > MAX_DECODED:
        raise ResourceLimitError("DECODED_BYTES_LIMIT: cumulative RGB extent exceeds admission limit")


def snapshot(path, target):
    """Copy once so metadata, decoder and identity refer to identical source bytes."""
    h = sha256()
    size = 0
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise ValueError("source must be a nonempty regular file")
        if info.st_size > MAX_INPUT:
            raise ResourceLimitError("INPUT_BYTES_LIMIT: source file exceeds admission limit")
        with target.open("xb") as out:
            while chunk := source.read(1 << 20):
                size += len(chunk)
                if size > info.st_size:
                    raise ValueError("source changed during admission")
                h.update(chunk)
                out.write(chunk)
        if size != info.st_size or info.st_mtime_ns != os.fstat(source.fileno()).st_mtime_ns:
            raise ValueError("source changed during admission")
    return h.hexdigest(), size


def _probe(path, options, *, output="json", on_stdout=None):
    errors = bytearray()
    raw = run_child(["/usr/bin/ffprobe", "-v", "error", *INPUT_OPTIONS, *options,
                     "-of", output, str(path)], on_stdout=on_stdout, on_stderr=errors.extend)
    if errors:
        raise ValueError("decode probe reported damaged or unsupported media")
    if on_stdout is None:
        return json.loads(raw)


def _discarded(packet):
    return "D" in packet.get("flags", "")


def _probe_rows(path, index, extent, entries, *, packets=False):
    """Count selected presentation rows as they arrive, without truncating the witness."""
    rows, count, pending = [], 0, b""
    selected = ["-select_streams", str(index), "-show_entries", entries]
    if packets:
        selected = ["-fflags", "+nofillin", *selected]

    def consume(chunk):
        nonlocal pending, count
        *lines, pending = (pending + chunk).split(b"\n")
        for line in lines:
            row = {}
            for field in line.decode("ascii").split("|"):
                name, _, value = field.partition("=")
                if name == "flags":
                    row[name] = value
                elif name in {"stream_index", "pts", "dts", "width", "height"} and value != "N/A":
                    row[name] = int(value)
            if not packets or not _discarded(row):
                count += 1
                check_extent(*extent, count)
            rows.append(row)

    _probe(path, selected, output="compact=p=0:nk=0", on_stdout=consume)
    if pending:
        raise ValueError("truncated metadata row from decoder probe")
    return rows


def check_packet_pts(packets, index, frame_pts, *, ffv1_block_time=False):
    """Require a complete multiset join with unsynthesized demuxer packet PTS."""
    packet_pts = []
    basis = "packet_pts"
    for packet in packets:
        if _discarded(packet):  # Decoder preroll outside the presentation.
            continue
        tick = packet.get("pts")
        # Matroska's VFW FFV1 demuxer exposes the block timestamp as DTS
        # with nofillin. FFV1 has no frame reordering. Require the complete
        # block sequence itself to equal the decoded presentation sequence.
        if tick is None and ffv1_block_time:
            tick = packet.get("dts")
            basis = "matroska_ffv1_block_timestamp_as_dts"
        if type(tick) is not int or packet.get("stream_index") != index:
            raise ValueError("missing container packet PTS or stream identity")
        packet_pts.append(tick)
    joined = packet_pts if basis != "packet_pts" else sorted(packet_pts)
    if joined != frame_pts:
        raise ValueError("incomplete container packet/frame PTS join")
    return basis


def probe(path, source_sha, byte_size):
    info = _probe(path, ["-select_streams", "v", "-show_entries",
                         "stream=index,width,height,time_base,codec_name,codec_tag_string,nb_frames:"
                         "stream_disposition=attached_pic:format=format_name,duration:format_tags=major_brand"])
    streams = info["streams"]
    if len(streams) != 1 or streams[0]["disposition"]["attached_pic"]:
        raise ValueError("ambiguous or absent video stream")
    stream = streams[0]
    w, h, index = stream["width"], stream["height"], stream["index"]
    check_extent(w, h, 1)
    base = Fraction(stream["time_base"])
    if base <= 0:
        raise ValueError("missing positive stream time base")
    container = info["format"]["format_name"]
    mov = "mov" in container.split(",")
    declared = stream.get("nb_frames")
    declared = None if declared in (None, "N/A") else int(declared)
    if declared is not None and not mov:
        check_extent(w, h, declared)

    # Demux before requesting frame metadata, which requires a decoder.
    # An edit list may retain discarded samples in the MOV sample-table count.
    packets = _probe_rows(path, index, (w, h), "packet=stream_index,pts,dts,flags", packets=True)
    presented = sum(not _discarded(packet) for packet in packets)
    check_extent(w, h, presented)
    if declared is not None and declared != (len(packets) if mov else presented):
        raise ValueError("incomplete container frame count")
    frames = _probe_rows(path, index, (w, h), "frame=pts,width,height,stream_index")
    pts = []
    for f in frames:
        if (f.get("stream_index"), f.get("width"), f.get("height")) != (index, w, h):
            raise ValueError("stream or geometry epoch changed")
        tick = f.get("pts")
        if type(tick) is not int or (pts and tick < pts[-1]):
            raise ValueError("missing or decreasing presentation timestamp")
        pts.append(tick)
    ffv1 = ("matroska" in container.split(",") and stream["codec_name"] == "ffv1"
            and stream.get("codec_tag_string") == "FFV1")
    packet_clock = check_packet_pts(packets, index, pts, ffv1_block_time=ffv1)
    if mov:
        brand = info["format"].get("tags", {}).get("major_brand", "").strip()
        mime = "video/quicktime" if brand == "qt" else "video/mp4"
    else:
        # Both document types use the same FFmpeg demuxer. Read the EBML
        # DocType element from the bounded header before choosing a subtype.
        with path.open("rb") as handle:
            header = handle.read(4096)
        mime = "video/webm" if b"\x42\x82\x84webm" in header else "video/x-matroska"
    duration = info["format"].get("duration")
    duration_ms = None if duration in (None, "N/A") else float(Fraction(duration) * 1000)
    version = run_child(["/usr/bin/ffmpeg", "-version"], max_stdout=65536)
    return Track("file", mime, index, base, w, h, pts, stream["codec_name"], container,
                 "pts", duration_ms, version.splitlines()[0].decode(), source_sha, byte_size, packet_clock)


_CLOCK = re.compile(rb"config in time_base:\s*(\d+/\d+)")
_FRAME = re.compile(rb"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+.*?fmt:rgb24\s+.*?\bs:(\d+)x(\d+)\s+.*?\bchecksum:([0-9A-Fa-f]{8})\b")


def decode(path, track, accept):
    """Pair showinfo after RGB conversion with byte-exact rawvideo on stdout.

    Verify both pipes before the private staging directory can be published.
    No labels, source-adjacent sidecars or inferred timestamps participate.
    """
    size = track.width * track.height * 3
    pixels, checks = bytearray(), []
    log = bytearray()

    def consume(data):
        pixels.extend(data)
        while len(pixels) >= size:
            i = len(checks)
            if i >= len(track.pts):
                raise ValueError("extra decoded pixels")
            frame = bytes(memoryview(pixels)[:size])
            del pixels[:size]
            checks.append(zlib.adler32(frame, 0))
            accept(i, frame)

    run_child(["/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "info",
               "-xerror", "-err_detect", "explode", "-copyts", "-noautorotate", *INPUT_OPTIONS,
               "-i", str(path), "-map", f"0:{track.index}", "-filter_threads", "1",
               "-vf", "sws_flags=bicubic;format=rgb24,showinfo", "-threads", "1", "-fps_mode", "passthrough",
               "-c:v", "rawvideo", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
              max_stdout=size * (len(track.pts) + 1), max_stderr=16 << 20, timeout=180,
              on_stdout=consume, on_stderr=log.extend)
    if pixels or len(checks) != len(track.pts):
        raise ValueError("incomplete decoded RGB frame join")
    seen, clock_seen = 0, False
    for line in log.splitlines():
        if b"File ended prematurely" in line:
            raise ValueError("incomplete container")
        if not line.startswith(b"[Parsed_showinfo_"):
            continue
        if match := _CLOCK.search(line):
            if Fraction(match[1].decode()) != track.base:
                raise ValueError("decoder time base differs from stream")
            clock_seen = True
        elif match := _FRAME.search(line):
            n, pts, w, h = map(int, match.groups()[:4])
            if (seen >= len(checks) or (n, pts, w, h, int(match[5], 16)) !=
                    (seen, track.pts[seen], track.width, track.height, checks[seen])):
                raise ValueError("decoder pixel/PTS witness mismatch")
            seen += 1
    if not clock_seen or seen != len(checks):
        raise ValueError("incomplete decoder timestamp witnesses")
