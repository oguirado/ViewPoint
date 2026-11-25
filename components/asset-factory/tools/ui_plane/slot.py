# SPDX-License-Identifier: AGPL-3.0-only
"""Make relocatable UI plates for a local compositor."""

import argparse
from bisect import bisect_right
from contextlib import contextmanager, nullcontext
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw


SCHEMA = "af.ui-plane/v1"
LEGACY_SCHEMA = "rp3.ui-plane/v1"
BACKEND = "pillow-cpu"
RUNTIME_ENV = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC"}
MAX_PIXELS = 16_777_216
MAX_FRAMES = 1_000_000
ORIENT = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


class PlaneError(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def require(condition, code, detail):
    if not condition:
        raise PlaneError(code, detail)


def digest(data):
    return sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def fields(value, names, label):
    require(isinstance(value, dict) and set(value) == set(names.split()), "invalid_request", label)


def integer(value):
    return type(value) is int


def number(value):
    return type(value) in (int, float) and -1e9 <= value <= 1e9


def vector(value, length, predicate, label):
    require(isinstance(value, list) and len(value) == length and all(map(predicate, value)),
            "invalid_request", label)
    return value


def rect(value, predicate, label):
    x, y, width, height = vector(value, 4, predicate, label)
    require(width > 0 and height > 0, "invalid_request", label)
    return x, y, width, height


def contains(bounds, inner):
    x, y, width, height = bounds
    left, top, w, h = inner
    return x <= left and y <= top and left + w <= x + width and top + h <= y + height


def positive_pair(value, label):
    return vector(value, 2, lambda n: integer(n) and 0 < n <= 1_000_000_000, label)


def safe_media_path(root, name):
    require(isinstance(name, str) and "\\" not in name and
            all(part not in ("", ".", "..") for part in name.split("/")),
            "invalid_request", "source path must be a portable relative file path")
    try:
        path = (root / name).resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise PlaneError("source_missing", name) from error
    require(path.is_relative_to(root), "invalid_request", "source path leaves request directory")
    require(path.is_file(), "source_missing", name)
    return path


def media_path(root, entry, *, packaged):
    path = safe_media_path(root, entry["path"])
    if packaged:
        require(path == root / entry["path"], "invalid_request", "package media uses a symlink")
    return path


def source_bytes(root, entry, *, packaged=False):
    path = media_path(root, entry, packaged=packaged)
    try:
        payload = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError) as error:
        raise PlaneError("source_missing", entry["path"]) from error
    require(digest(payload) == entry["sha256"], "source_hash_mismatch", entry["path"])
    return payload


def copy_media(root, entry, target, *, packaged=False):
    """Copy and check a pin in one pass, including videos too large to load into RAM."""
    path = media_path(root, entry, packaged=packaged)
    checksum = sha256()
    try:
        with path.open("rb") as source, target.open("xb") as output:
            while block := source.read(1024 * 1024):
                checksum.update(block)
                output.write(block)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise PlaneError("source_missing", entry["path"]) from error
    require(checksum.hexdigest() == entry["sha256"], "source_hash_mismatch", entry["path"])


def decode(payload, name, orientation):
    try:
        with Image.open(BytesIO(payload)) as image:
            require(image.format in {"PNG", "JPEG", "WEBP"} and getattr(image, "n_frames", 1) == 1,
                    "unsupported_media", name)
            require(image.width * image.height <= MAX_PIXELS, "unsupported_media", name)
            converted = image.convert("RGBA")
    except (OSError, Image.DecompressionBombError) as error:
        raise PlaneError("unsupported_media", name) from error
    return converted.transpose(ORIENT[orientation]) if orientation in ORIENT else converted


def extract_video(root, source, count, work):
    """Extract presentation-order frames by ordinal from a verified local snapshot."""
    entry = source["media"]
    snapshot = work / "input"
    copy_media(root, entry, snapshot, packaged=True)
    start = source["start_frame"]
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-xerror",
        "-filter_threads", "1", "-threads", "1", "-max_pixels", str(MAX_PIXELS),
        "-noautorotate", "-protocol_whitelist", "file",
        "-format_whitelist", "mov,mp4,matroska,webm",
        "-i", str(snapshot), "-map", "0:V:0",
        "-vf", f"select=gte(n\\,{start})", "-frames:v", str(count),
        "-fps_mode", "passthrough", "-c:v", "png", "-pix_fmt", "rgba", "-threads", "1",
        "-f", "image2", "-start_number", "0", str(work / "%06d.png"),
    ]
    try:
        finished = subprocess.run(command, env={**os.environ, **RUNTIME_ENV}, timeout=120,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError as error:
        raise PlaneError("backend_unavailable", "ffmpeg") from error
    except subprocess.TimeoutExpired as error:
        raise PlaneError("video_timeout", entry["path"]) from error
    require(finished.returncode == 0, "unsupported_media", entry["path"])
    frames = [work / f"{index:06}.png" for index in range(count)]
    require(all(frame.is_file() for frame in frames), "video_frame_count_mismatch", entry["path"])
    return frames


def keyframes(rows, value_fields, interval, label, *, allow_empty=False):
    require(isinstance(rows, list) and (rows or allow_empty), "invalid_request", label)
    for row in rows:
        fields(row, f"frame {value_fields}", label)
        require(integer(row["frame"]), "invalid_request", label)
    frames = [row["frame"] for row in rows]
    if frames:
        require(frames == sorted(set(frames)) and interval[0] <= frames[0]
                and frames[-1] < interval[1], "invalid_request", label)


def fraction(value):
    return Fraction(str(value))


def interpolate(rows, frame, values):
    index = bisect_right([row["frame"] for row in rows], frame)
    if index == 0:
        return values(rows[0])
    if index == len(rows):
        return values(rows[-1])
    first, second = rows[index - 1], rows[index]
    progress = Fraction(frame - first["frame"], second["frame"] - first["frame"])
    return tuple(a + (b - a) * progress for a, b in zip(values(first), values(second)))


def view_values(row):
    return (*map(fraction, row["center"]), fraction(row["zoom"]))


def cursor_values(row):
    return tuple(map(fraction, row["position"]))


def nearest(value):
    return math.floor(value + Fraction(1, 2))


def media_entries(source):
    return [source["media"]] if source["kind"] == "video" else source["frames"]


def inspect_request(request, *, packaged):
    fields(request, "schema backend canvas viewport fps interval source crop view "
                    "protected min_ui_scale cursor", "request")
    require(request["schema"] in (SCHEMA, LEGACY_SCHEMA), "invalid_request", "schema")
    require(request["backend"] == BACKEND, "unsupported_backend", str(request["backend"]))
    canvas = positive_pair(request["canvas"], "canvas")
    require(canvas[0] * canvas[1] <= MAX_PIXELS, "invalid_request", "canvas pixel count")
    viewport = rect(request["viewport"], integer, "viewport")
    require(contains((0, 0, *canvas), viewport), "invalid_request", "viewport outside canvas")
    positive_pair(request["fps"], "fps")
    start, end = vector(request["interval"], 2, integer, "interval")
    require(0 <= start < end and end - start <= MAX_FRAMES, "invalid_request", "interval")
    rect(request["crop"], integer, "crop")
    require(number(request["min_ui_scale"]) and request["min_ui_scale"] > 0,
            "invalid_request", "min_ui_scale")

    source = request["source"]
    require(isinstance(source, dict), "invalid_request", "source")
    kind = source.get("kind")
    if kind == "video" and request["schema"] == SCHEMA:
        fields(source, "kind pixel_aspect orientation media start_frame", "source")
        first = source["start_frame"]
        require(integer(first) and 0 <= first <= MAX_FRAMES - (end - start),
                "invalid_request", "source.start_frame")
    else:
        fields(source, "kind pixel_aspect orientation frames", "source")
        require(kind in ("still", "clip"), "invalid_request", "source.kind")
        expected = 1 if kind == "still" else end - start
        require(isinstance(source["frames"], list) and len(source["frames"]) == expected,
                "invalid_request", "source.frames does not cover the interval")
    positive_pair(source["pixel_aspect"], "source.pixel_aspect")
    require(integer(source["orientation"]) and 1 <= source["orientation"] <= 8,
            "invalid_request", "source.orientation")
    for entry in media_entries(source):
        fields(entry, "path sha256", "source frame")
        pin = entry["sha256"]
        require(isinstance(pin, str) and re.fullmatch(r"[a-f0-9]{64}", pin),
                "invalid_request", "source frame sha256")
        require(isinstance(entry["path"], str), "invalid_request", "source frame path")
        if packaged:
            require(entry["path"] == f"media/{pin}", "invalid_request", "noncanonical package path")

    protected = request["protected"]
    require(isinstance(protected, list) and protected, "invalid_request", "protected")
    for region in protected:
        fields(region, "id rect", "protected region")
        require(isinstance(region["id"], str) and bool(region["id"].strip()),
                "invalid_request", "protected region id")
        rect(region["rect"], integer, "protected region")

    view = request["view"]
    keyframes(view, "center zoom", request["interval"], "view")
    for row in view:
        vector(row["center"], 2, number, "view.center")
        require(number(row["zoom"]) and row["zoom"] >= 1, "invalid_request", "view.zoom")
    cursor = request["cursor"]
    keyframes(cursor, "position", request["interval"], "cursor", allow_empty=True)
    for row in cursor:
        vector(row["position"], 2, number, "cursor.position")


def source_dimensions(request, root, video):
    source = request["source"]
    orientation = source["orientation"]
    if video is not None:
        # The decoder already read the compressed stream. Check each generated raster header.
        encoded_size = None
        for path in video:
            with Image.open(path) as image:
                if encoded_size is None:
                    encoded_size = image.size
                require(image.size == encoded_size, "source_geometry_mismatch", source["media"]["path"])
        return encoded_size[::-1] if orientation >= 5 else encoded_size

    size = None
    for entry in {row["path"]: row for row in source["frames"]}.values():
        image = decode(source_bytes(root, entry, packaged=True), entry["path"], orientation)
        if size is None:
            size = image.size
        require(size == image.size, "source_geometry_mismatch", entry["path"])
    return size


def cursor_radius(request):
    if request["schema"] == LEGACY_SCHEMA:
        return 7
    return max(3, nearest(Fraction(7 * min(request["canvas"]), 260)))


def plan_frame(request, frame):
    center_x, center_y, zoom = interpolate(request["view"], frame, view_values)
    width = Fraction(request["crop"][2]) / zoom
    height = Fraction(request["crop"][3]) / zoom
    crop = (center_x - width / 2, center_y - height / 2, width, height)
    left, top, _, _ = crop
    where = f"frame {frame}"
    require(contains(request["crop"], crop), "crop_out_of_bounds", where)

    pixel_aspect = Fraction(*request["source"]["pixel_aspect"])
    if request["source"]["orientation"] >= 5:
        pixel_aspect = 1 / pixel_aspect
    vx, vy, vw, vh = request["viewport"]
    k = min(vw / (width * pixel_aspect), vh / height)
    dw, dh = nearest(width * pixel_aspect * k), nearest(height * k)
    require(dw > 0 and dh > 0, "crop_unreadable", where)
    dx, dy = vx + (vw - dw) // 2, vy + (vh - dh) // 2
    sx, sy = dw / width, dh / height

    protected = []
    for region in request["protected"]:
        rx, ry, rw, rh = region["rect"]
        require(contains(crop, region["rect"]), "crop_off_target", f'{where}: {region["id"]}')
        protected.append((dx + (rx - left) * sx, dy + (ry - top) * sy, rw * sx, rh * sy))
    require(min(sx, sy) >= fraction(request["min_ui_scale"]), "crop_unreadable", where)

    cursor = None
    radius = None
    if request["cursor"]:
        px, py = interpolate(request["cursor"], frame, cursor_values)
        cx = math.floor(dx + (px - left) * sx)
        cy = math.floor(dy + (py - top) * sy)
        radius = cursor_radius(request)
        left, top = cx - radius, cy - radius
        right, bottom = cx + radius, cy + radius
        require(contains((dx, dy, dw, dh), (left, top, 2 * radius + 1, 2 * radius + 1)),
                "cursor_off_plane", where)
        for rx, ry, rw, rh in protected:
            clears = left >= rx + rw or top >= ry + rh
            if request["schema"] == LEGACY_SCHEMA:
                clears = clears or right < rx or bottom < ry
            else:
                clears = clears or right + 1 <= rx or bottom + 1 <= ry
            require(clears, "cursor_obscures_ui", where)
        cursor = (cx, cy)

    return {"frame": frame, "crop": crop, "plane": (dx, dy, dw, dh), "cursor": cursor,
            "cursor_radius": radius}


def frame_plan(request, size):
    require(contains((0, 0, *size), request["crop"]), "crop_out_of_bounds", "base crop")
    return [plan_frame(request, frame) for frame in range(*request["interval"])]


def load_request(path, *, packaged):
    def unique(pairs):
        result = dict(pairs)
        require(len(result) == len(pairs), "invalid_request", "duplicate JSON member")
        return result

    payload = path.read_bytes()
    try:
        request = json.loads(payload, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PlaneError("invalid_request", str(error)) from error
    inspect_request(request, packaged=packaged)
    return request, digest(payload)


@contextmanager
def new_directory(destination):
    destination = Path(destination)
    require(not destination.exists() and not destination.is_symlink(),
            "output_exists", destination.name)
    with TemporaryDirectory(prefix=".plane-", dir=destination.parent) as scratch:
        stage = Path(scratch) / "stage"
        stage.mkdir()
        yield stage
        require(not destination.exists() and not destination.is_symlink(),
                "output_exists", destination.name)
        stage.rename(destination)


def pack(draft, destination):
    """Validate a draft and publish a self-contained input package."""
    draft = Path(draft).resolve(strict=True)
    request, _ = load_request(draft, packaged=False)
    with new_directory(destination) as package:
        (package / "media").mkdir()
        entries = media_entries(request["source"])
        checked = {}
        for entry in entries:
            original = entry["path"]
            pin = entry["sha256"]
            target = package / "media" / pin
            if original not in checked:
                # Different draft paths with the same digest still need verification.
                if target.exists():
                    source_bytes(draft.parent, entry)
                else:
                    copy_media(draft.parent, entry, target)
                checked[original] = pin
            require(checked[original] == pin, "source_hash_mismatch", original)
            entry["path"] = f"media/{pin}"
        (package / "request.json").write_bytes(json_bytes(request))
        verification = verify_package(package, scratch_parent=package.parent)
    return verification


@contextmanager
def open_package(package, *, scratch_parent=None):
    root = Path(package).resolve(strict=True)
    request_path = root / "request.json"
    require(not request_path.is_symlink() and request_path.is_file(),
            "invalid_request", "package request must be a regular file")
    request, request_pin = load_request(request_path, packaged=True)
    source = request["source"]
    scratch_dir = (TemporaryDirectory(prefix=".decode-", dir=scratch_parent)
                   if source["kind"] == "video" else nullcontext())
    with scratch_dir as scratch:
        start, stop = request["interval"]
        video = extract_video(root, source, stop - start, Path(scratch)) if scratch else None
        size = source_dimensions(request, root, video)
        plan = frame_plan(request, size)
        verification = {
            "schema": "rp3.ui-plane-verified/v1",
            "backend": BACKEND,
            "request_sha256": request_pin,
            "source": source,
            "oriented_size": size,
            "canvas": request["canvas"],
            "interval": request["interval"],
            "fps": request["fps"],
        }
        yield root, request, plan, verification, video


def verify_package(package, *, scratch_parent=None):
    """Reopen all pinned media and verify the complete timeline."""
    with open_package(package, scratch_parent=scratch_parent) as (_, _, _, verification, _):
        return verification


def compose(image, canvas, geometry):
    dx, dy, dw, dh = geometry["plane"]
    x, y, width, height = geometry["crop"]
    box = tuple(map(float, (x, y, x + width, y + height)))
    plate = Image.new("RGBA", canvas)
    if box[0].is_integer() and box[1].is_integer() and (width, height) == (dw, dh):
        layer = image.crop(tuple(map(int, box)))
    else:
        layer = image.resize((dw, dh), Image.Resampling.LANCZOS, box=box)
    plate.paste(layer, (dx, dy))
    if geometry["cursor"] is not None:
        cx, cy = geometry["cursor"]
        draw = ImageDraw.Draw(plate)
        radius = geometry["cursor_radius"]
        stroke = max(1, nearest(Fraction(2 * radius, 7)))
        for r, color in ((radius, "#152030"), (radius - stroke, "#ffdd35")):
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=stroke)
        dot = (stroke - 1) // 2
        draw.rectangle((cx - dot, cy - dot, cx + dot, cy + dot), fill="#ffdd35")
    return plate


def render_package(package, destination):
    """Render ordered RGBA PNGs and return the digest-bound render receipt."""
    with new_directory(destination) as output:
        with open_package(package, scratch_parent=output.parent) as prepared:
            root, request, plan, verification, video = prepared
            rows = render_frames(root, request, plan, output, video)
        receipt = {**verification, "schema": "rp3.ui-plane-render/v1", "frames": rows}
        (output / "receipt.json").write_bytes(json_bytes(receipt))
    return receipt


def render_frames(root, request, plan, output, video):
    source = request["source"]
    orientation = source["orientation"]
    last_pin, image = None, None
    rows = []
    (output / "frames").mkdir()
    for index, geometry in enumerate(plan):
        if video is not None:
            pin = source["media"]["sha256"]
            image = decode(video[index].read_bytes(), source["media"]["path"], orientation)
        else:
            entry = source["frames"][0 if source["kind"] == "still" else index]
            pin = entry["sha256"]
            if pin != last_pin:
                image = decode(source_bytes(root, entry, packaged=True), entry["path"], orientation)
                last_pin = pin
        plate = compose(image, request["canvas"], geometry)
        relative = f"frames/{index:06}.png"
        payload = BytesIO()
        plate.save(payload, format="PNG")
        pixels = payload.getvalue()
        (output / relative).write_bytes(pixels)
        row = {
            "frame": geometry["frame"], "path": relative, "sha256": digest(pixels),
            "source_sha256": pin, "crop": list(map(str, geometry["crop"])),
            "plane": geometry["plane"], "cursor": geometry["cursor"],
        }
        if request["schema"] == SCHEMA:
            row["cursor_radius"] = geometry["cursor_radius"]
        if video is not None:
            row["source_frame"] = source["start_frame"] + index
        rows.append(row)
    return rows


def main():
    os.environ.update(RUNTIME_ENV)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack_parser = commands.add_parser("pack")
    pack_parser.add_argument("draft")
    pack_parser.add_argument("package")
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("package")
    render_parser = commands.add_parser("render")
    render_parser.add_argument("package")
    render_parser.add_argument("output")
    args = parser.parse_args()
    try:
        if args.command == "pack":
            result = pack(args.draft, args.package)
        elif args.command == "verify":
            result = verify_package(args.package)
        else:
            result = render_package(args.package, args.output)
    except PlaneError as error:
        print(json.dumps({"error": error.code, "detail": str(error)}), file=sys.stderr)
        return 2
    except OSError as error:
        print(json.dumps({"error": "io_error", "detail": str(error)}), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(json_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
