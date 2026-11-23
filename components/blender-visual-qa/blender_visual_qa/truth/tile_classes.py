"""Compact RLE persistence for per-tile S2/S3 engine classes (bvqa.tile-cls/v1).

Reads vision_dir/tiles.bin (offset layout.TILE_HDR_SIZE, dtype desc.TILE_DTYPE,
field cls) together with vision_dir/frames.jsonl and writes vision_dir/tile_cls.jsonl
atomically as compact text (never a frame).
"""
import hashlib
import re
import sys
from pathlib import Path

import numpy as np

sys.dont_write_bytecode = True
from . import pin as _pin  # noqa: E402

_pin.pin()
from visioncore import canon, desc, layout  # noqa: E402
from visioncore.errors import VqaError  # noqa: E402
from visioncore.grid import Grid  # noqa: E402

SCHEMA = "bvqa.tile-cls/v1"
FILE_NAME = "tile_cls.jsonl"
TILE_CLS_NAME = FILE_NAME
CLS_COUNT = len(layout.CLS)
_PAIR_RE = re.compile(r"^([0-6]):([1-9][0-9]*)$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def encode_rle(cls):
    """Encode one frame of uint8 classes in Morton tile order as 'cls:count,...' pairs."""
    arr = np.asarray(cls)
    if arr.ndim != 1 or arr.dtype == bool or not np.issubdtype(arr.dtype, np.integer):
        raise VqaError("E_SCHEMA", {"reason": "cls must be a 1-D integer array"})
    if arr.size == 0:
        return ""
    lo, hi = int(arr.min()), int(arr.max())
    if lo < 0 or hi >= CLS_COUNT:
        raise VqaError("E_SCHEMA", {"reason": "cls out of range", "min": lo, "max": hi})
    u8 = np.ascontiguousarray(arr, dtype=np.uint8)
    cuts = np.flatnonzero(u8[1:] != u8[:-1]) + 1
    starts = np.concatenate(([0], cuts))
    counts = np.concatenate((cuts, [u8.size])) - starts
    return ",".join("%d:%d" % (int(u8[s]), int(n)) for s, n in zip(starts, counts))


def decode_rle(rle, n_tiles=None):
    """Decode a run-length string of 'cls:count' pairs into a 1-D uint8 class array."""
    if not isinstance(rle, str):
        raise VqaError("E_SCHEMA", {"reason": "rle must be a string"})
    if n_tiles is not None:
        if isinstance(n_tiles, bool) or not isinstance(n_tiles, (int, np.integer)) or int(n_tiles) < 0:
            raise VqaError("E_SCHEMA", {"reason": "invalid n_tiles"})
        n_tiles = int(n_tiles)
    if not rle:
        out = np.zeros(0, dtype=np.uint8)
    else:
        vals, counts = [], []
        for token in rle.split(","):
            m = _PAIR_RE.match(token)
            if m is None:
                raise VqaError("E_SCHEMA", {"reason": "invalid rle pair", "token": token[:32]})
            vals.append(int(m.group(1)))
            counts.append(int(m.group(2)))
        out = np.repeat(np.asarray(vals, dtype=np.uint8), np.asarray(counts, dtype=np.int64))
    if n_tiles is not None and int(out.size) != n_tiles:
        raise VqaError("E_SCHEMA", {"reason": "rle tile count mismatch", "have": int(out.size), "want": n_tiles})
    return out


encode = encode_rle
decode = decode_rle
encode_frame = encode_rle
decode_frame = decode_rle


def frame_record(frame, grid, cls):
    """Build one bvqa.tile-cls/v1 record dict for a single frame of uint8 classes."""
    if isinstance(frame, bool) or not isinstance(frame, (int, np.integer)) or int(frame) < 0:
        raise VqaError("E_SCHEMA", {"reason": "invalid frame id"})
    ident = grid.ident if hasattr(grid, "ident") else str(grid)
    rle = encode_rle(cls)
    u8 = np.ascontiguousarray(cls, dtype=np.uint8)
    return {
        "schema": SCHEMA,
        "frame": int(frame),
        "grid": ident,
        "n_tiles": int(u8.size),
        "rle": rle,
        "sha256": hashlib.sha256(u8.tobytes()).hexdigest(),
    }


def write_tile_classes(vision_dir):
    """Read vision_dir/tiles.bin + frames.jsonl and write vision_dir/tile_cls.jsonl atomically."""
    vdir = Path(vision_dir)
    tiles_path = vdir / "tiles.bin"
    frames_path = vdir / "frames.jsonl"
    if not tiles_path.is_file() or not frames_path.is_file():
        raise VqaError("E_IO", {"path": str(vdir), "reason": "missing tiles.bin or frames.jsonl"})
    data = tiles_path.read_bytes()
    frame_rows = [canon.loads(line) for line in frames_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not frame_rows:
        raise VqaError("E_VC_FORMAT", {"reason": "empty frames.jsonl"})
    hdr = layout.parse_tile_header(data)
    grid = Grid(hdr["W"], hdr["H"])
    if (hdr["s"], hdr["gw"], hdr["gh"], hdr["N"]) != (grid.s, grid.gw, grid.gh, grid.N):
        raise VqaError("E_VC_FORMAT", {"reason": "tile header grid mismatch"})
    if hdr["n_frames"] != len(frame_rows):
        raise VqaError("E_VC_FORMAT", {"reason": "tile dimensions mismatch"})
    expected_len = layout.TILE_HDR_SIZE + len(frame_rows) * grid.N * layout.TILE_SIZE
    if len(data) != expected_len:
        raise VqaError("E_VC_FORMAT", {"reason": "tiles.bin size mismatch", "have": len(data), "want": expected_len})
    tiles = np.frombuffer(data, dtype=desc.TILE_DTYPE, offset=layout.TILE_HDR_SIZE).reshape(len(frame_rows), grid.N)
    records = []
    for idx, f_row in enumerate(frame_rows):
        if f_row.get("schema") != "vqa.core-frame/v1" or f_row.get("grid") != grid.ident:
            raise VqaError("E_SCHEMA", {"reason": "frame schema/grid mismatch"})
        frame_id = f_row.get("frame")
        if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id != hdr["f0"] + idx:
            raise VqaError("E_SCHEMA", {"reason": "frame sequence mismatch"})
        cls_u8 = np.ascontiguousarray(tiles[idx]["cls"], dtype=np.uint8)
        if "n" in f_row:
            if np.bincount(cls_u8, minlength=CLS_COUNT).tolist() != f_row["n"]:
                raise VqaError("E_SCHEMA", {"reason": "frame class histogram mismatch", "frame": frame_id})
        records.append(frame_record(frame_id, grid.ident, cls_u8))
    out_path = vdir / FILE_NAME
    canon.write_atomic(out_path, "".join(canon.dumps(r) for r in records))
    return out_path


def read_tile_classes(path_or_dir, grid=None, frame_ids=None):
    """Read and verify tile_cls.jsonl -> uint8[F, N] class array."""
    p = Path(path_or_dir)
    if p.is_dir():
        p = p / FILE_NAME
    if not p.is_file():
        raise VqaError("E_IO", {"path": str(p), "reason": "absent tile_cls.jsonl"})
    rows = [canon.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise VqaError("E_SCHEMA", {"reason": "empty tile_cls.jsonl"})
    ident = (grid.ident if hasattr(grid, "ident") else str(grid)) if grid is not None else rows[0].get("grid")
    n_tiles = grid.N if hasattr(grid, "N") else rows[0].get("n_tiles")
    if isinstance(n_tiles, bool) or not isinstance(n_tiles, int) or n_tiles <= 0:
        raise VqaError("E_SCHEMA", {"reason": "invalid n_tiles"})
    if frame_ids is not None and [r.get("frame") for r in rows] != list(frame_ids):
        raise VqaError("E_SCHEMA", {"reason": "frame ids mismatch"})
    decoded = []
    for idx, row in enumerate(rows):
        if row.get("schema") != SCHEMA or row.get("grid") != ident or row.get("n_tiles") != n_tiles:
            raise VqaError("E_SCHEMA", {"reason": "tile_cls metadata mismatch"})
        f = row.get("frame")
        if isinstance(f, bool) or not isinstance(f, int) or (frame_ids is None and f != rows[0]["frame"] + idx):
            raise VqaError("E_SCHEMA", {"reason": "tile_cls frame ordering mismatch"})
        sha = row.get("sha256")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise VqaError("E_SCHEMA", {"reason": "invalid tile_cls sha256"})
        cls_u8 = decode_rle(row.get("rle"), n_tiles=n_tiles)
        if hashlib.sha256(cls_u8.tobytes()).hexdigest() != sha:
            raise VqaError("E_SCHEMA", {"reason": "tile_cls sha256 mismatch", "frame": f})
        decoded.append(cls_u8)
    return np.stack(decoded, axis=0)
