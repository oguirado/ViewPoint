"""PX mutants (vqa.px-mutant/v1): ops toggle_rect, freeze_rect, paste_rect, shift_rect, scroll_rect.
Truth fields come from ids.field_ids_of_rect, never from the engine under test."""
import numpy as np

from visioncore import ids
from visioncore.errors import VqaError

OPS = ("toggle_rect", "freeze_rect", "paste_rect", "shift_rect", "scroll_rect")


def _clip(rect, W, H):
    x, y, w, h = rect
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > W or y + h > H:
        raise VqaError("E_USAGE", {"rect": list(rect)})
    return x, y, w, h


def _truth(rect, frames, W, H):
    fields = []
    for f in frames:
        fields += ids.field_ids_of_rect(rect, W, H, f)
    return {"rect": list(rect), "frames": list(frames), "fields": fields}


def apply_op(frames, op, rect, params=None):
    """frames: list of uint8 (H,W,3). Returns (new_frames, mutant dict). Inputs are never modified.
    params: frames=[...] affected frame indices (default all), plus per op:
    toggle_rect {} (invert) | freeze_rect {from_frame} | paste_rect {src:[x,y]} | shift_rect {dx,dy} | scroll_rect {dx,dy per frame}."""
    if op not in OPS:
        raise VqaError("E_USAGE", {"op": op})
    params = dict(params or {})
    H, W = frames[0].shape[:2]
    x, y, w, h = _clip(rect, W, H)
    sel = sorted(set(params.get("frames", range(len(frames)))))
    out = [f.copy() for f in frames]
    if op == "toggle_rect":
        for i in sel:
            out[i][y:y + h, x:x + w] = 255 - frames[i][y:y + h, x:x + w]
    elif op == "freeze_rect":
        f0 = params["from_frame"]
        sel = [i for i in sel if i > f0]
        for i in sel:
            out[i][y:y + h, x:x + w] = frames[f0][y:y + h, x:x + w]
    elif op == "paste_rect":
        sx, sy = params["src"]
        _clip((sx, sy, w, h), W, H)
        for i in sel:
            out[i][y:y + h, x:x + w] = frames[i][sy:sy + h, sx:sx + w]
    elif op == "shift_rect":
        dx, dy = params["dx"], params["dy"]
        for i in sel:
            out[i][y:y + h, x:x + w] = np.roll(frames[i][y:y + h, x:x + w], (dy, dx), axis=(0, 1))
    else:  # scroll_rect: cumulative wrap-around scroll of rect content
        dx, dy = params["dx"], params["dy"]
        for k, i in enumerate(sel, 1):
            out[i][y:y + h, x:x + w] = np.roll(frames[i][y:y + h, x:x + w], (dy * k, dx * k), axis=(0, 1))
    return out, {"schema": "vqa.px-mutant/v1", "op": op, "params": params, "truth": _truth((x, y, w, h), sel, W, H)}
