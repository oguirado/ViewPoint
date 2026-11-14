# @module {"id":"wa.perceive.vision_adapter","layer":2,"owner":"PERCEIVE","deps":["wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.vision_adapter - vision-engine bridge adapter, stdlib fake, and STATE region fusion.

Frames live only in RAM as raw RGB24 bytes (STORAGE LAW: never written to disk); bounds are [TARGET, unit decision].
Real hosted adapter boundary (not a v0 stub); persistent server mode is AUSENTE (ABSENT),
so C2 subprocess adapters pay one-shot spawn latency [TARGET, unit decision]. Integer-only.
"""
import abc
from collections import deque
from dataclasses import dataclass
import hashlib

from ..state import schema


_IOU_OMITTED = object()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# @anchor:wa.perceive.vision_adapter.Frame
@dataclass(frozen=True)
class Frame:
    """In-memory RGB24 row-major frame buffer."""

    width: int
    height: int
    data: bytes

    def __post_init__(self):
        if not _valid_frame_dimensions(self.width, self.height):
            raise ValueError("Frame width and height must be positive integers")
        if not isinstance(self.data, bytes):
            raise ValueError("Frame data must be bytes")
        if len(self.data) != self.width * self.height * 3:
            raise ValueError(
                "Frame data length %d != %d" % (len(self.data), self.width * self.height * 3)
            )


# @anchor:wa.perceive.vision_adapter.VisionEngineAdapter
class VisionEngineAdapter(abc.ABC):
    """Narrow interface between Wender PERCEIVE and a pixel-diff engine."""

    @abc.abstractmethod
    def screen_identity(self, frame):
        """Return a 16-character lowercase hex fingerprint for `frame`."""
        raise NotImplementedError

    @abc.abstractmethod
    def changed_regions(self, prev, cur):
        """Return merged changed regions `[{'x','y','w','h','tiles'}, ...]` sorted by y then x."""
        raise NotImplementedError


# @anchor:wa.perceive.vision_adapter.FakeVisionEngine
class FakeVisionEngine(VisionEngineAdapter):
    """Deterministic integer-only stdlib implementation of VisionEngineAdapter."""

    def __init__(self, tile=16, threshold=8):
        if not _is_int(tile) or tile <= 0:
            raise ValueError("tile must be a positive integer")
        if not _is_int(threshold) or threshold < 0:
            raise ValueError("threshold must be a non-negative integer")
        self.tile = tile
        self.threshold = threshold

    def screen_identity(self, frame):
        if not isinstance(frame, Frame):
            raise ValueError("frame must be a Frame")
        digest = hashlib.sha256()
        digest.update(("%d:%d:" % (frame.width, frame.height)).encode("ascii"))
        stride = frame.width * 3
        data = frame.data
        tile = self.tile
        for y0 in range(0, frame.height, tile):
            y1 = min(y0 + tile, frame.height)
            for x0 in range(0, frame.width, tile):
                x1 = min(x0 + tile, frame.width)
                sum_r = 0
                sum_g = 0
                sum_b = 0
                for y in range(y0, y1):
                    row_start = y * stride + x0 * 3
                    row_end = y * stride + x1 * 3
                    sum_r += sum(data[row_start:row_end:3])
                    sum_g += sum(data[row_start + 1:row_end:3])
                    sum_b += sum(data[row_start + 2:row_end:3])
                pixels = (x1 - x0) * (y1 - y0)
                digest.update(
                    bytes((sum_r // pixels, sum_g // pixels, sum_b // pixels))
                )
        return digest.hexdigest()[:16]

    def changed_regions(self, prev, cur):
        if not isinstance(prev, Frame) or not isinstance(cur, Frame):
            raise ValueError("prev and cur must be Frame instances")
        if (prev.width, prev.height) != (cur.width, cur.height):
            raise ValueError("Frame dimensions do not match")
        width, height = prev.width, prev.height
        stride = width * 3
        tile = self.tile
        threshold = self.threshold
        n_rows = (height + tile - 1) // tile
        n_cols = (width + tile - 1) // tile
        changed = set()
        prev_data = prev.data
        cur_data = cur.data

        for ty in range(n_rows):
            y0 = ty * tile
            y1 = min(y0 + tile, height)
            for tx in range(n_cols):
                x0 = tx * tile
                x1 = min(x0 + tile, width)
                total_diff = 0
                for y in range(y0, y1):
                    start = y * stride + x0 * 3
                    end = y * stride + x1 * 3
                    for a, b in zip(prev_data[start:end], cur_data[start:end]):
                        total_diff += abs(a - b)
                channels = (x1 - x0) * (y1 - y0) * 3
                if total_diff > 0 and (total_diff // channels) >= threshold:
                    changed.add((ty, tx))

        visited = set()
        regions = []
        for start_tile in sorted(changed):
            if start_tile in visited:
                continue
            queue = deque([start_tile])
            visited.add(start_tile)
            comp = []
            while queue:
                cy, cx = queue.popleft()
                comp.append((cy, cx))
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if (ny, nx) in changed and (ny, nx) not in visited:
                        visited.add((ny, nx))
                        queue.append((ny, nx))
            min_ty = min(t[0] for t in comp)
            max_ty = max(t[0] for t in comp)
            min_tx = min(t[1] for t in comp)
            max_tx = max(t[1] for t in comp)
            x0 = min_tx * tile
            y0 = min_ty * tile
            x1 = min(width, (max_tx + 1) * tile)
            y1 = min(height, (max_ty + 1) * tile)
            regions.append({
                "x": x0,
                "y": y0,
                "w": x1 - x0,
                "h": y1 - y0,
                "tiles": len(comp),
            })

        regions.sort(key=lambda r: (r["y"], r["x"], r["h"], r["w"], r["tiles"]))
        return regions


# Keep the earlier pixel-difference AST coordinates stable for existing audits.
# [TARGET, unit decision] Planner-parity axes; the pixel ceiling admits
# 7680x4320 and bounds each RGB buffer to 99532800 bytes.
MAX_FRAME_DIMENSION = 16384
MAX_FRAME_PIXELS = 33_177_600


def _valid_frame_dimensions(width, height):
    if not _is_int(width) or not _is_int(height) or width <= 0 or height <= 0:
        return False
    if width > MAX_FRAME_DIMENSION or height > MAX_FRAME_DIMENSION:
        raise ValueError("Frame dimension exceeds limit")
    if width * height > MAX_FRAME_PIXELS:
        raise ValueError("Frame pixel count exceeds limit")
    return True


def _target_identity(value):
    """Compare target provenance structurally, across independently built records."""
    try:
        kind = value["kind"] if isinstance(value, dict) else value.kind
        ident = value["canonical_id"] if isinstance(value, dict) else value.canonical_id
        gen = value["generation"] if isinstance(value, dict) else value.generation
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("target handle binding missing") from exc
    if (not isinstance(kind, str) or not kind or not isinstance(ident, str)
            or not ident or type(gen) is not int or gen < 0):
        raise ValueError("invalid target handle binding")
    return kind, ident, gen


# @anchor:wa.perceive.vision_adapter.fuse_regions
def fuse_regions(state, regions, dpr_ppm=1000000, *,
                 iou_num=_IOU_OMITTED, iou_den=_IOU_OMITTED,
                 handle=None, epoch=None):
    """Floor both device endpoints into CSS px and intersect STATE node boxes.

    Omitted IoU terms retain every positive overlap. Supply both integer terms
    to filter by ``intersection / union >= iou_num / iou_den`` without floats.
    Explicit ``None`` is invalid, as is supplying only one term.
    Unlike detector outward rounding, both fusion endpoints use floor division;
    empty axes stay empty, and positive physical spans may legitimately collapse.
    """
    if not _is_int(dpr_ppm) or dpr_ppm <= 0:
        raise ValueError("dpr_ppm must be a positive integer")
    use_iou = iou_num is not _IOU_OMITTED or iou_den is not _IOU_OMITTED
    if use_iou and (
        type(iou_num) is not int or type(iou_den) is not int
        or iou_den <= 0 or not 0 <= iou_num <= iou_den
    ):
        raise ValueError("IoU requires integer 0 <= iou_num <= iou_den and iou_den > 0")
    # Explicit batch target binding is mandatory whenever a caller supplies a
    # handle or geometry epoch. A wrapper keeps strict legacy STATE untouched:
    # {"state": STATE, "handle": Handle, "epoch": int, "dpr_ppm": int}.
    if handle is not None or epoch is not None:
        if not isinstance(state, dict):
            raise ValueError("target-bound fusion requires a state envelope")
        binding = state
        if handle is not None:
            if "handle" not in binding or _target_identity(binding["handle"]) != _target_identity(handle):
                raise ValueError("fusion AX namespace differs from target handle")
        if epoch is not None:
            if (type(epoch) is not int or epoch < 0 or "epoch" not in binding
                    or type(binding["epoch"]) is not int or binding["epoch"] != epoch):
                raise ValueError("fusion geometry epoch differs from target")
        if ("dpr_ppm" not in binding or type(binding["dpr_ppm"]) is not int
                or binding["dpr_ppm"] != dpr_ppm):
            raise ValueError("fusion DPR differs from bound target")
        state = binding.get("state", binding)
        if not isinstance(state, dict):
            raise ValueError("bound AX state must be a dict")
        for node in state.get("nodes", ()):
            if not isinstance(node, dict):
                raise ValueError("bound AX nodes must be dicts")
            if handle is not None and "handle" in node and _target_identity(node["handle"]) != _target_identity(handle):
                raise ValueError("sibling target AX node in fusion state")
            if epoch is not None and "epoch" in node and node["epoch"] != epoch:
                raise ValueError("stale geometry AX node in fusion state")
        viewport = state.get("viewport")
        if (isinstance(viewport, dict) and "dpr_ppm" in viewport
                and viewport["dpr_ppm"] != dpr_ppm):
            raise ValueError("AX viewport DPR differs from target DPR")
    if not isinstance(state, dict) or not isinstance(state.get("nodes"), list):
        raise ValueError("state must contain a nodes list")
    if "schema" in state:
        errs = schema.validate_state(state)
        if errs:
            raise schema.StateError("; ".join(errs))

    hits = []
    unmatched = []
    nodes = state["nodes"]

    for idx, reg in enumerate(regions):
        if not isinstance(reg, dict):
            raise ValueError("region must be a dict")
        for key in ("x", "y", "w", "h"):
            if not _is_int(reg.get(key)):
                raise ValueError("region.%s must be an integer" % key)
        if reg["w"] < 0 or reg["h"] < 0:
            raise ValueError("region dimensions must be non-negative")
        rx = (reg["x"] * 1000000) // dpr_ppm
        ry = (reg["y"] * 1000000) // dpr_ppm
        rx1 = ((reg["x"] + reg["w"]) * 1000000) // dpr_ppm
        ry1 = ((reg["y"] + reg["h"]) * 1000000) // dpr_ppm
        rw = rx1 - rx
        rh = ry1 - ry
        matched = False

        for node in nodes:
            box = node.get("box")
            if box is None:
                continue
            nx, ny, nw, nh = box
            ix0 = max(rx, nx)
            iy0 = max(ry, ny)
            ix1 = min(rx1, nx + nw)
            iy1 = min(ry1, ny + nh)
            if ix1 > ix0 and iy1 > iy0:
                overlap = (ix1 - ix0) * (iy1 - iy0)
                union = rw * rh + nw * nh - overlap
                if union <= 0 or (use_iou and overlap * iou_den < union * iou_num):
                    continue
                matched = True
                hits.append({
                    "region": idx,
                    "node": node["id"],
                    "overlap": overlap,
                    "actionable": bool(node["actionable"]),
                })

        if not matched:
            unmatched.append(idx)

    hits.sort(key=lambda h: (h["region"], not h["actionable"], -h["overlap"], h["node"]))
    result = {
        "hits": hits,
        "unmatched_regions": unmatched,
    }
    schema.dumps(result)
    return result


BRIDGE_STATUS = "real"
PERSISTENT_MODE = "vserve"
VisionEngineAdapter.bridge_status = BRIDGE_STATUS
VisionEngineAdapter.persistent_mode = PERSISTENT_MODE
