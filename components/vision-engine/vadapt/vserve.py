# @module {"id":"va.vserve","layer":4,"owner":"ARCH","deps":["vc.decode","vc.errors","va.cu_describe","va.ramws","va.rtring"],"book":"HB-VC-s2-change"}
"""Persistent C2 JSON-lines server on stdin/stdout with bounded control, frame, and session memory.

[TARGET, unit decision] Wire protocol (`ve.vserve.reply/v1`):
- CLI: `python3 -m vadapt.vserve [--max-control-line-bytes N] [--max-frame-bytes M] [--max-sessions K]`
- Input: UTF-8 JSON objects, one per line on binary stdin, bounded by `max_control_line_bytes` (including LF).
  Oversized lines are drained in bounded chunks without accumulation and refused with `E_BUDGET` (rc 6).
- Request schema:
  `{"request_id": <pos-int|str>, "op": "describe"|"hello"|"ping"|"capabilities"|"open_session"|"close_session"|"reap"|"status", ...}`
  If `op` is omitted and a frame key (`frame`, `frame_raw`, `ring`) is present, `op` defaults to `"describe"`.
- Reply schema (one canonical JSON line per request):
  `{"code": <str>, "detail": <dict>, "ok": <bool>, "op": <str|null>, "rc": <0|2|3|4|5|6>, "request_id": <id|null>, "result": <dict|null>, "schema": "ve.vserve.reply/v1"}`
  `rc` is never 1. All budget caps (`max_control_line_bytes`, `max_frame_bytes`, `max_sessions`) refuse with `E_BUDGET` (rc 6).
"""
import hashlib
import json
import os
from pathlib import Path
import posix
import re
import stat
import struct
import sys
import time
import zlib
from io import DEFAULT_BUFFER_SIZE

import numpy as np

from visioncore.decode import png
from visioncore.errors import CODES, RC_CLASSES, VqaError
from vnative.adapters.cu_regions import pair_regions, pair_regions_plus
from vadapt import cu_describe, ramws, rtring
from vadapt.cu_marks import candidates, validate_ram_path, write_crops, write_marks
from vadapt.cu_ocr import annotate_marks as ocr_annotate_marks

DEFAULT_MAX_CONTROL_LINE_BYTES = 65536
DEFAULT_MAX_FRAME_BYTES = 33554432
DEFAULT_MAX_SESSIONS = 16

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OPS = frozenset({
    "describe", "Describe",
    "hello", "ping", "capabilities",
    "open_session", "close_session", "reap", "status",
})
_DESCRIBE_KEYS = frozenset({
    "op", "request_id", "session_id",
    "frame", "prev",
    "frame_raw", "prev_raw",
    "ring", "prev_ring",
    "use_session_prev",
    "masks", "mask",
    "marks_out", "crop_dir",
    "s4_attrs", "marks_extra", "ocr_marks", "tessdata_dir",
})


def _positive_int(value, name, maximum=sys.maxsize - 1, code="E_USAGE"):
    if type(value) is not int or value <= 0 or (maximum is not None and value > maximum):
        raise VqaError(code, {"field": name, "reason": f"{name} must be a positive integer in range"})
    return value


def _valid_request_id(value):
    if type(value) is int and 0 < value <= sys.maxsize:
        return True
    if type(value) is str and 0 < len(value) <= 128 and value.isprintable():
        return True
    return False


def _session_name(value, code="E_SCHEMA"):
    if type(value) is not str or _SESSION_RE.fullmatch(value) is None:
        raise VqaError(code, {"field": "session_id", "reason": "invalid session_id"})
    return value


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise VqaError("E_SCHEMA", {"reason": "duplicate JSON key", "key": key})
        out[key] = value
    return out


def _constant(value):
    raise VqaError("E_SCHEMA", {"reason": "nonfinite JSON number", "value": str(value)})


def _parse_masks(raw_masks):
    if raw_masks is None:
        return ()
    if not isinstance(raw_masks, (list, tuple)):
        raise VqaError("E_SCHEMA", {"field": "masks", "reason": "masks must be a list"})
    out = []
    for item in raw_masks:
        if isinstance(item, str):
            parts = item.split(",")
            if len(parts) != 4:
                raise VqaError("E_SCHEMA", {"field": "masks", "reason": "mask string must be x,y,w,h"})
            try:
                vals = [int(p) for p in parts]
            except ValueError:
                raise VqaError("E_SCHEMA", {"field": "masks", "reason": "mask coordinates must be integers"}) from None
        elif isinstance(item, (list, tuple)) and len(item) == 4 and all(type(v) is int for v in item):
            vals = [int(v) for v in item]
        elif isinstance(item, dict) and set(item) == {"x", "y", "w", "h"} and all(type(item[k]) is int for k in ("x", "y", "w", "h")):
            vals = [item["x"], item["y"], item["w"], item["h"]]
        else:
            raise VqaError("E_SCHEMA", {"field": "masks", "reason": "invalid mask entry"})
        x, y, w, h = vals
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            raise VqaError("E_SCHEMA", {"field": "masks", "reason": "mask must have x,y >= 0 and w,h > 0"})
        out.append((x, y, w, h))
    return tuple(out)


def _check_shm_path(raw_path):
    if type(raw_path) is not str or not raw_path or "\0" in raw_path or not os.path.isabs(raw_path):
        raise VqaError("E_IO", {"path": str(raw_path), "reason": "absolute path required"})
    parts = raw_path.split("/")
    if "." in parts or ".." in parts:
        raise VqaError("E_IO", {"path": raw_path, "reason": "path traversal refused"})
    resolved = Path(raw_path).resolve()
    if not resolved.is_relative_to("/dev/shm") or ramws.is_ram_fs(resolved) not in ramws.RAM_FS:
        raise VqaError("E_IO", {"path": raw_path, "reason": "path must reside on verified /dev/shm tmpfs"})
    if os.path.islink(raw_path):
        raise VqaError("E_IO", {"path": raw_path, "reason": "symlink refused"})
    return resolved


def _load_png_bytes(path, max_frame_bytes):
    if type(path) is not str or not path or "\0" in path:
        raise VqaError("E_SCHEMA", {"field": "frame", "reason": "frame path must be a non-empty string"})
    try:
        with open(path, "rb") as fh:
            limit = max_frame_bytes + 1
            # Size the first allocation for this PNG, using the budget only as a cap.
            size = min(limit, max(DEFAULT_BUFFER_SIZE, os.fstat(fh.fileno()).st_size + 1))
            data = fh.read(size)
            if len(data) == size and size < limit:
                # The file may have grown since fstat, or have no reported size.
                chunks = [data]
                remaining = limit - size
                while remaining:
                    size = min(DEFAULT_BUFFER_SIZE, remaining)
                    chunk = fh.read(size)
                    chunks.append(chunk)
                    if len(chunk) < size:
                        break
                    remaining -= size
                data = b"".join(chunks)
    except OSError as exc:
        raise VqaError("E_IO", {"path": path, "reason": str(exc)[:80]}) from None
    if len(data) > max_frame_bytes:
        raise VqaError("E_BUDGET", {"cap": "max_frame_bytes", "limit": max_frame_bytes, "bytes": len(data)})
    if not data:
        raise VqaError("E_VC_PNG", {"reason": "empty png"})
    if data[:8] == _PNG_SIG and len(data) >= 24 and data[12:16] == b"IHDR":
        w, h = struct.unpack_from(">II", data, 16)
        if 1 <= w <= 8192 and 1 <= h <= 8192 and w * h * 3 > max_frame_bytes:
            raise VqaError("E_BUDGET", {"cap": "max_frame_bytes", "limit": max_frame_bytes, "bytes": w * h * 3})
    return data


def _load_raw_rgb(spec, max_frame_bytes):
    if isinstance(spec, str):
        parts = spec.rsplit(",", 2)
        if len(parts) != 3:
            raise VqaError("E_SCHEMA", {"field": "frame_raw", "reason": "expected <shm-path>,<w>,<h>"})
        raw_path = parts[0]
        try:
            width, height = int(parts[1]), int(parts[2])
        except ValueError:
            raise VqaError("E_SCHEMA", {"field": "frame_raw", "reason": "width and height must be integers"}) from None
    elif isinstance(spec, dict):
        raw_path = spec.get("path")
        width = spec.get("width", spec.get("w"))
        height = spec.get("height", spec.get("h"))
    else:
        raise VqaError("E_SCHEMA", {"field": "frame_raw", "reason": "frame_raw must be str or dict"})
    _positive_int(width, "width", maximum=8192, code="E_SCHEMA")
    _positive_int(height, "height", maximum=8192, code="E_SCHEMA")
    _check_shm_path(raw_path)
    want = width * height * 3
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = posix.open(raw_path, flags)
    except OSError as exc:
        raise VqaError("E_IO", {"path": str(raw_path), "reason": str(exc)[:80]}) from None
    try:
        st = posix.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise VqaError("E_IO", {"path": str(raw_path), "reason": "regular file required"})
        if st.st_size != want:
            raise VqaError("E_VC_FRAME_SIZE", {"path": str(raw_path), "bytes": int(st.st_size), "expected": want})
        if want > max_frame_bytes:
            raise VqaError("E_BUDGET", {"cap": "max_frame_bytes", "limit": max_frame_bytes, "bytes": want})
        chunks, remaining = [], want
        while remaining > 0:
            chunk = posix.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        buf = b"".join(chunks)
    except OSError as exc:
        raise VqaError("E_IO", {"path": str(raw_path), "reason": str(exc)[:80]}) from None
    finally:
        posix.close(fd)
    if len(buf) != want:
        raise VqaError("E_VC_FRAME_SIZE", {"path": str(raw_path), "bytes": len(buf), "expected": want})
    return np.frombuffer(buf, dtype=np.uint8).copy().reshape(height, width, 3)


def _load_ring_rgb(spec, max_frame_bytes):
    if isinstance(spec, str):
        parts = spec.rsplit(",", 1)
        if len(parts) != 2:
            raise VqaError("E_SCHEMA", {"field": "ring", "reason": "expected <ring-path>,<index>"})
        ring_path = parts[0]
        try:
            index = int(parts[1])
        except ValueError:
            raise VqaError("E_SCHEMA", {"field": "ring", "reason": "ring index must be an integer"}) from None
    elif isinstance(spec, dict):
        ring_path = spec.get("path")
        index = spec.get("index")
    else:
        raise VqaError("E_SCHEMA", {"field": "ring", "reason": "ring must be str or dict"})
    _positive_int(index, "index", code="E_SCHEMA")
    _check_shm_path(ring_path)
    try:
        reader = rtring.RingReader(ring_path, root="/dev/shm")
    except (OSError, ValueError, struct.error, ramws.RamError) as exc:
        raise VqaError("E_IO", {"path": str(ring_path), "reason": str(exc)[:80]}) from None
    try:
        frame_bytes = int(reader.w) * int(reader.h) * 3
        if frame_bytes > max_frame_bytes:
            raise VqaError("E_BUDGET", {"cap": "max_frame_bytes", "limit": max_frame_bytes, "bytes": frame_bytes})
        arr = reader.read(lambda view: np.array(view, dtype=np.uint8, copy=True), index=index)
    finally:
        reader.close()
    if arr is None:
        raise VqaError("E_STALE", {"path": str(ring_path), "index": index})
    return arr


def _describe_rgb(current, previous=None, masks=(), marks_out=None, *,
                  s4_attrs=False, marks_extra=False, crop_dir=None,
                  ocr_marks=False, tessdata_dir=None, start_ns=None):
    """Run the C2 description pipeline on decoded uint8[H,W,3] arrays."""
    if ocr_marks and marks_out is None:
        raise VqaError("E_USAGE", {"reason": "--ocr-marks requires --marks-out"})
    start = time.perf_counter_ns() if start_ns is None else start_ns
    try:
        if marks_out is not None:
            validate_ram_path(marks_out)
        if crop_dir is not None:
            validate_ram_path(crop_dir, is_dir=True)
    except ValueError as exc:
        raise VqaError("E_USAGE", {"reason": str(exc)}) from None
    height, width, _ = current.shape
    digest = hashlib.sha256(current.tobytes()).hexdigest()
    found = []
    s4_adjacency = []
    if previous is not None:
        if previous.shape != current.shape:
            raise VqaError("E_VC_FRAME_SHAPE", {"reason": "frame and prev dimensions differ"})
        compared = current.copy() if masks else current
        for x, y, w, h in masks:
            compared[y:y + h, x:x + w] = previous[y:y + h, x:x + w]
        if s4_attrs:
            records, raw_adj = pair_regions_plus(previous, compared)
            s4_adjacency = [[str(a), str(b), int(c)] for a, b, c in raw_adj]
        else:
            records = pair_regions(previous, compared)
        dirty_tiles = sum(rec["n_tiles"] for rec in records)
        for rec in records:
            x, y, w, h = (int(v) for v in rec["bbox_px"])
            entry = dict(x=x, y=y, w=w, h=h, score=float(rec["n_tiles"]) / dirty_tiles)
            if s4_attrs:
                entry["s4"] = dict(
                    group=str(rec["group"]), cls=str(rec["cls"]), kind=str(rec["kind"]),
                    mv=[int(v) for v in rec["mv"]], edge_ppm=int(rec["edge_ppm"]),
                    r_max=int(rec["r_max"]), n_tiles=int(rec["n_tiles"]),
                    shade_only=bool(rec["shade_only"]))
            found.append(entry)
    found.sort(key=lambda r: (-r["score"], r["y"], r["x"], r["h"], r["w"]))
    found = found[:16]
    crops = [dict(x=r["x"], y=r["y"], w=r["w"], h=r["h"], reason="changed")
             for r in found[:8]]
    result = dict(
        schema="ve.cu_describe/v1",
        frame_sha256=digest,
        width=int(width),
        height=int(height),
        changed=None if previous is None else bool(found),
        changed_regions=found,
        crops=crops,
        timing_us=(time.perf_counter_ns() - start) // 1000,
    )
    if s4_attrs:
        result["s4_adjacency"] = s4_adjacency
    if crop_dir is not None:
        result.update(crop_dir=str(crop_dir), crop_pngs=write_crops(crop_dir, current, crops))
        result["timing_us"] = (time.perf_counter_ns() - start) // 1000
    if marks_out is not None:
        marks = candidates(current, extra=bool(marks_extra or s4_attrs))
        write_marks(marks_out, current, marks)
        if ocr_marks:
            marks = ocr_annotate_marks(current, marks, tessdata_dir=tessdata_dir, clean_sha256=digest)
        result.update(marks=marks, marks_png=str(marks_out))
        result["timing_us"] = (time.perf_counter_ns() - start) // 1000
    return result


class VServeEngine:
    """Bounded worker state for persistent C2 JSON-lines serving."""

    def __init__(self, *, max_control_line_bytes=DEFAULT_MAX_CONTROL_LINE_BYTES,
                 max_frame_bytes=DEFAULT_MAX_FRAME_BYTES,
                 max_sessions=DEFAULT_MAX_SESSIONS):
        self.max_control_line_bytes = _positive_int(max_control_line_bytes, "max_control_line_bytes")
        self.max_frame_bytes = _positive_int(max_frame_bytes, "max_frame_bytes")
        self.max_sessions = _positive_int(max_sessions, "max_sessions")
        self._sessions = {}
        self.closed = False

    def _ensure_session(self, session_id):
        _session_name(session_id)
        if session_id in self._sessions:
            return self._sessions[session_id]
        if len(self._sessions) >= self.max_sessions:
            raise VqaError("E_BUDGET", {"cap": "max_sessions", "limit": self.max_sessions})
        entry = {"session_id": session_id, "frame_rgb": None, "frame_sha256": None, "bytes": 0}
        self._sessions[session_id] = entry
        return entry

    def session_bytes(self):
        return sum(s["bytes"] for s in self._sessions.values())

    def close(self):
        reaped = sorted(self._sessions)
        self._sessions.clear()
        self.closed = True
        return {"reaped": reaped, "retained": [], "active_sessions": 0}

    def _resolve_input(self, obj, prefix=""):
        key_png = prefix + "frame" if not prefix else prefix.rstrip("_")
        key_raw = prefix + "frame_raw" if not prefix else "prev_raw"
        key_ring = prefix + "ring" if not prefix else "prev_ring"
        present = [k for k in (key_png, key_raw, key_ring) if obj.get(k) is not None]
        if len(present) > 1:
            raise VqaError("E_SCHEMA", {"reason": f"mutually exclusive frame keys: {present}"})
        if not present:
            return None, None
        kind = present[0]
        val = obj[kind]
        if kind == key_png:
            if isinstance(val, dict):
                return "rgb", _load_raw_rgb(val, self.max_frame_bytes)
            data = _load_png_bytes(val, self.max_frame_bytes)
            return "png", data
        if kind == key_raw:
            return "rgb", _load_raw_rgb(val, self.max_frame_bytes)
        return "rgb", _load_ring_rgb(val, self.max_frame_bytes)

    def _handle_describe(self, obj):
        unknown = set(obj) - _DESCRIBE_KEYS
        if unknown:
            raise VqaError("E_SCHEMA", {"reason": "unknown describe fields", "fields": sorted(unknown)})
        for flag in ("s4_attrs", "marks_extra", "ocr_marks", "use_session_prev"):
            if flag in obj and type(obj[flag]) is not bool:
                raise VqaError("E_SCHEMA", {"field": flag, "reason": "boolean required"})
        marks_out = obj.get("marks_out")
        crop_dir = obj.get("crop_dir")
        tessdata_dir = obj.get("tessdata_dir")
        for path_field, val in (("marks_out", marks_out), ("crop_dir", crop_dir), ("tessdata_dir", tessdata_dir)):
            if val is not None and (type(val) is not str or not val or "\0" in val):
                raise VqaError("E_SCHEMA", {"field": path_field, "reason": "non-empty string path required"})
        ocr_marks = bool(obj.get("ocr_marks", False))
        if ocr_marks and marks_out is None:
            raise VqaError("E_USAGE", {"reason": "--ocr-marks requires --marks-out"})
        try:
            if marks_out is not None:
                validate_ram_path(marks_out)
            if crop_dir is not None:
                validate_ram_path(crop_dir, is_dir=True)
        except ValueError as exc:
            raise VqaError("E_USAGE", {"reason": str(exc)}) from None

        raw_masks = obj.get("masks", obj.get("mask"))
        masks = _parse_masks(raw_masks)

        session = None
        sid = obj.get("session_id")
        if sid is not None:
            session = self._ensure_session(sid)

        start_ns = time.perf_counter_ns()
        cur_kind, cur_val = self._resolve_input(obj, prefix="")
        if cur_kind is None:
            raise VqaError("E_SCHEMA", {"field": "frame", "reason": "missing frame, frame_raw, or ring"})

        use_session_prev = bool(obj.get("use_session_prev", False))
        if obj.get("prev") == "@session":
            use_session_prev = True
            obj = dict(obj, prev=None)

        prev_kind, prev_val = self._resolve_input(obj, prefix="prev_")
        if prev_kind is None and use_session_prev and session is not None and session["frame_rgb"] is not None:
            prev_kind, prev_val = "rgb", session["frame_rgb"]

        s4_attrs = bool(obj.get("s4_attrs", False))
        marks_extra = bool(obj.get("marks_extra", False))

        if cur_kind == "png" and prev_kind in (None, "png") and session is None:
            try:
                return cu_describe.describe(
                    cur_val, prev_val, masks, marks_out,
                    s4_attrs=s4_attrs, marks_extra=marks_extra,
                    crop_dir=crop_dir, ocr_marks=ocr_marks,
                    tessdata_dir=tessdata_dir,
                )
            except VqaError:
                raise
            except (struct.error, zlib.error) as exc:
                raise VqaError("E_VC_PNG", {"reason": str(exc)[:80]}) from None
            except ValueError as exc:
                msg = str(exc)
                code = "E_VC_FRAME_SHAPE" if "dimensions differ" in msg else "E_USAGE"
                raise VqaError(code, {"reason": msg[:120]}) from None
            except OSError as exc:
                raise VqaError("E_IO", {"reason": str(exc)[:80]}) from None

        cur_rgb = png(cur_val) if cur_kind == "png" else cur_val
        if cur_rgb.nbytes > self.max_frame_bytes:
            raise VqaError("E_BUDGET", {"cap": "max_frame_bytes", "limit": self.max_frame_bytes, "bytes": int(cur_rgb.nbytes)})
        prev_rgb = (png(prev_val) if prev_kind == "png" else prev_val) if prev_kind is not None else None
        if prev_rgb is not None and prev_rgb.nbytes > self.max_frame_bytes:
            raise VqaError("E_BUDGET", {"cap": "max_frame_bytes", "limit": self.max_frame_bytes, "bytes": int(prev_rgb.nbytes)})

        result = _describe_rgb(
            cur_rgb, prev_rgb, masks, marks_out,
            s4_attrs=s4_attrs, marks_extra=marks_extra,
            crop_dir=crop_dir, ocr_marks=ocr_marks,
            tessdata_dir=tessdata_dir, start_ns=start_ns,
        )
        if session is not None:
            session["frame_rgb"] = cur_rgb
            session["frame_sha256"] = result["frame_sha256"]
            session["bytes"] = int(cur_rgb.nbytes)
        return result

    def handle(self, obj):
        if type(obj) is not dict:
            raise VqaError("E_SCHEMA", {"reason": "request must be a JSON object"})
        if "request_id" not in obj or not _valid_request_id(obj["request_id"]):
            raise VqaError("E_SCHEMA", {"field": "request_id", "reason": "valid request_id required"})
        op = obj.get("op")
        if op is None and any(k in obj for k in ("frame", "frame_raw", "ring")):
            op = "describe"
        if type(op) is not str or op not in _OPS:
            raise VqaError("E_SCHEMA", {"field": "op", "reason": "unknown or missing op"})

        if op in ("hello", "ping", "capabilities"):
            extra = set(obj) - {"op", "request_id"}
            if extra:
                raise VqaError("E_SCHEMA", {"reason": "unexpected fields", "fields": sorted(extra)})
            return {
                "schema": "ve.vserve.hello/v1",
                "capabilities": ["describe", "png", "frame_raw", "ring", "sessions"],
                "raw_shm": True,
                "frame_raw": True,
                "ring": True,
                "max_control_line_bytes": self.max_control_line_bytes,
                "max_frame_bytes": self.max_frame_bytes,
                "max_sessions": self.max_sessions,
                "active_sessions": len(self._sessions),
            }

        if op == "open_session":
            extra = set(obj) - {"op", "request_id", "session_id"}
            if extra:
                raise VqaError("E_SCHEMA", {"reason": "unexpected fields", "fields": sorted(extra)})
            entry = self._ensure_session(obj.get("session_id"))
            return {
                "session_id": entry["session_id"],
                "active_sessions": len(self._sessions),
                "session_bytes": self.session_bytes(),
            }

        if op == "close_session":
            extra = set(obj) - {"op", "request_id", "session_id"}
            if extra:
                raise VqaError("E_SCHEMA", {"reason": "unexpected fields", "fields": sorted(extra)})
            sid = _session_name(obj.get("session_id"))
            removed = self._sessions.pop(sid, None) is not None
            return {
                "session_id": sid,
                "closed": removed,
                "active_sessions": len(self._sessions),
                "session_bytes": self.session_bytes(),
            }

        if op == "reap":
            extra = set(obj) - {"op", "request_id", "session_id"}
            if extra:
                raise VqaError("E_SCHEMA", {"reason": "unexpected fields", "fields": sorted(extra)})
            sid = obj.get("session_id")
            if sid is not None:
                _session_name(sid)
                reaped = [sid] if self._sessions.pop(sid, None) is not None else []
            else:
                reaped = sorted(self._sessions)
                self._sessions.clear()
            return {
                "reaped": reaped,
                "active_sessions": len(self._sessions),
                "session_bytes": self.session_bytes(),
            }

        if op == "status":
            extra = set(obj) - {"op", "request_id"}
            if extra:
                raise VqaError("E_SCHEMA", {"reason": "unexpected fields", "fields": sorted(extra)})
            return {
                "active_sessions": len(self._sessions),
                "session_ids": sorted(self._sessions),
                "session_bytes": self.session_bytes(),
                "max_control_line_bytes": self.max_control_line_bytes,
                "max_frame_bytes": self.max_frame_bytes,
                "max_sessions": self.max_sessions,
            }

        return self._handle_describe(obj)


def _safe_rc(code, rc=None):
    resolved = CODES.get(code, 2) if rc is None else rc
    if resolved == 1 or resolved not in RC_CLASSES or resolved == 0:
        return 2
    return resolved


def serve(engine, input_binary, output_text, *, max_control_line_bytes=None):
    """Serve bounded JSON lines on input_binary/output_text.

    The byte limit includes LF. A final unterminated line within the cap is
    accepted. Oversized lines are drained in bounded chunks, not accumulated,
    and refused with E_BUDGET (rc 6). EOF closes the engine and emits no extra reply.
    """
    if engine is None:
        limit = DEFAULT_MAX_CONTROL_LINE_BYTES if max_control_line_bytes is None else max_control_line_bytes
        engine = VServeEngine(max_control_line_bytes=limit)
    limit = engine.max_control_line_bytes if max_control_line_bytes is None else max_control_line_bytes
    _positive_int(limit, "max_control_line_bytes", maximum=sys.maxsize - 1)
    try:
        while True:
            raw = input_binary.readline(limit + 1)
            if not raw:
                break
            request_id = None
            op = None
            result = None
            detail = {}
            code = "OK"
            rc = 0
            try:
                if not isinstance(raw, bytes):
                    raise VqaError("E_SCHEMA", {"reason": "binary input required"})
                if len(raw) > limit:
                    while not raw.endswith(b"\n"):
                        raw = input_binary.readline(min(limit + 1, 65536))
                        if not raw:
                            break
                    raise VqaError("E_BUDGET", {"cap": "max_control_line_bytes", "limit": limit})
                obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
                if type(obj) is dict:
                    rid = obj.get("request_id")
                    if _valid_request_id(rid):
                        request_id = rid
                    raw_op = obj.get("op")
                    if raw_op is None and any(k in obj for k in ("frame", "frame_raw", "ring")):
                        raw_op = "describe"
                    if type(raw_op) is str and raw_op in _OPS:
                        op = "describe" if raw_op == "Describe" else raw_op
                result = engine.handle(obj)
            except VqaError as exc:
                code = exc.code if exc.code in CODES else "E_SCHEMA"
                rc = _safe_rc(code, exc.rc)
                detail = dict(exc.detail or {})
            except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
                code = "E_SCHEMA"
                rc = 2
                detail = {"reason": str(exc)[:120]}
            except (struct.error, zlib.error) as exc:
                code = "E_VC_PNG"
                rc = 3
                detail = {"reason": str(exc)[:80]}
            except ramws.RamError as exc:
                code = "E_BUDGET" if exc.code == "E_RAM_QUOTA" else "E_IO"
                rc = 6 if code == "E_BUDGET" else 2
                detail = {"code": exc.code, "reason": str(exc)[:80]}
            except OSError as exc:
                code = "E_IO"
                rc = 2
                detail = {"reason": str(exc)[:80]}
            reply = {
                "code": code,
                "detail": detail,
                "ok": code == "OK",
                "op": op,
                "rc": rc,
                "request_id": request_id,
                "result": result,
                "schema": "ve.vserve.reply/v1",
            }
            output_text.write(json.dumps(reply, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            output_text.flush()
    finally:
        cleanup = engine.close()
    return cleanup


_CLI_OPTIONS = {
    "max-control-line-bytes": DEFAULT_MAX_CONTROL_LINE_BYTES,
    "max-frame-bytes": DEFAULT_MAX_FRAME_BYTES,
    "max-sessions": DEFAULT_MAX_SESSIONS,
}


def _parse_cli(argv):
    values = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        i += 1
        if not isinstance(token, str) or not token.startswith("--"):
            raise VqaError("E_USAGE", {"reason": "expected named option", "arg": str(token)})
        key, sep, value = token[2:].partition("=")
        if key not in _CLI_OPTIONS or key in values:
            raise VqaError("E_USAGE", {"reason": "unknown or repeated option: --" + key})
        if not sep:
            if i >= len(argv) or str(argv[i]).startswith("--"):
                raise VqaError("E_USAGE", {"reason": "missing option value: --" + key})
            value = argv[i]
            i += 1
        if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
            raise VqaError("E_USAGE", {"reason": "positive decimal integer required: --" + key})
        try:
            parsed = int(value)
        except ValueError:
            raise VqaError("E_USAGE", {"reason": "integer too long: --" + key}) from None
        _positive_int(parsed, key.replace("-", "_"), maximum=sys.maxsize - 1, code="E_USAGE")
        values[key] = parsed
    out = dict(_CLI_OPTIONS)
    out.update(values)
    return out


def main(argv=None, *, stdin=None, stdout=None, stderr=None):
    """CLI entrypoint for `python3 -m vadapt.vserve`; exits 0 on clean EOF, 2/3/4/5/6 on error, never 1."""
    argv = sys.argv[1:] if argv is None else list(argv)
    output = sys.stdout if stdout is None else stdout
    error = sys.stderr if stderr is None else stderr
    try:
        opts = _parse_cli(argv)
        engine = VServeEngine(
            max_control_line_bytes=opts["max-control-line-bytes"],
            max_frame_bytes=opts["max-frame-bytes"],
            max_sessions=opts["max-sessions"],
        )
        in_stream = sys.stdin.buffer if stdin is None else stdin
        serve(engine, in_stream, output, max_control_line_bytes=opts["max-control-line-bytes"])
        return 0
    except VqaError as exc:
        code = exc.code if exc.code in CODES else "E_USAGE"
        rc = _safe_rc(code, exc.rc)
        error.write(json.dumps({"code": code, "detail": exc.detail, "rc": rc},
                               sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        return rc
    except (OSError, ValueError, MemoryError) as exc:
        error.write(json.dumps({"code": "E_IO", "detail": {"reason": type(exc).__name__}, "rc": 2},
                               sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
