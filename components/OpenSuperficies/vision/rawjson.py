"""raw.json writer/validator for stored RGB24 captures (law2 GuiAdapter raw layout). Stdlib only, no engine import.

raw.json = {"width", "height", "stride", "file"} (closed, integers); the file holds frames back to back, stride*height bytes
each, ordered by render_index (the ring file is slot-wrapped, so its order is NOT capture order).
"""
import hashlib
import os
import re

LINEAR_FILE = "frames.linear.raw"
KEYS = ("file", "height", "stride", "width")
MAX_DIM = 1 << 15
_BASENAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")  # plain basename: no separator, quote or control char


class RawJsonError(ValueError):
    pass


def validate(obj):
    """Closed validator mirroring capture.ValidateRawJSON (Go): unknown key / non-int / bad geometry / bad path -> RawJsonError."""
    if not isinstance(obj, dict) or tuple(sorted(obj)) != KEYS:
        raise RawJsonError("raw_json_keys")
    w, h, s, f = obj["width"], obj["height"], obj["stride"], obj["file"]
    if any(type(v) is not int for v in (w, h, s)) or not (1 <= w <= MAX_DIM and 1 <= h <= MAX_DIM) or s < w * 3:
        raise RawJsonError("raw_json_geometry")
    if not isinstance(f, str) or not f or ".." in f or not _BASENAME.fullmatch(f):
        raise RawJsonError("raw_json_file")
    return obj


def linearise(frames):
    """[(render_index, rgb_bytes)] in any order -> rgb frames sorted ascending by render_index (duplicates refused)."""
    ris = [r for r, _ in frames]
    if len(set(ris)) != len(ris):
        raise RawJsonError("duplicate_render_index")
    return [b for _, b in sorted(frames, key=lambda x: x[0])]


def os_manifest(frames, w, h):
    """Minimal OS-side vqa.frame/v1 rows (i, render_index, sha256) for the capture, ascending by render_index."""
    rows = []
    for i, (ri, b) in enumerate(sorted(frames, key=lambda x: x[0])):
        hx = hashlib.sha256(b).hexdigest()
        rows.append({"i": i, "render_index": ri, "sha256": hx, "file_sha256": hx})
    return {"schema": "vqa.frame/v1", "width": w, "height": h, "channels": 3, "pixel_format": "rgb24",
            "storage": "rgb24_raw", "count": len(rows), "frames": rows}


def write_linear(run_dir, w, h, frames):
    """Write frames.linear.raw + raw.json into run_dir (a ramws path chosen by the caller). Returns the raw.json dict."""
    fb = w * h * 3
    if any(len(b) != fb for _, b in frames) or not frames:
        raise RawJsonError("frame_size")
    with open(os.path.join(run_dir, LINEAR_FILE), "wb") as fh:
        for b in linearise(frames):
            fh.write(b)
    rj = {"width": w, "height": h, "stride": w * 3, "file": LINEAR_FILE}
    with open(os.path.join(run_dir, "raw.json"), "w", encoding="utf-8") as fh:
        fh.write('{"file":"%s","height":%d,"stride":%d,"width":%d}\n' % (LINEAR_FILE, h, w * 3, w))
    return validate(rj)
