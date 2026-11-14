"""Runs synth frames through S0-S2 and maps truth fields to per-tile classes (stdlib+numpy)."""
import json
import re
from pathlib import Path

import numpy as np

from visioncore import change as C, desc
from visioncore.grid import Grid

SPEC_DIR = Path(__file__).resolve().parent
FIELD = re.compile(r"^f(0|[1-9][0-9]{0,9}):t([0-9a-f]{4})$")
CLS = ("UNCHANGED", "JITTER", "CHANGED", "MOVED", "FLICKER", "FROZEN", "DRIFT")


def load_specs():
    return {p.stem: json.loads(p.read_text()) for p in sorted(SPEC_DIR.glob("*.json"))}


def classify_frames(frames):
    """-> (grid, cls array [F,N] in Morton order, global_mv list, per-frame classified tiles)."""
    H, W = frames[0].shape[:2]
    g = Grid(W, H)
    pw = px = st = None
    out, gmv, tiles = [], [], []
    for f in frames:
        pad = g.pad(f)
        t, eq = desc.describe_padded(pad, g, pw, px)
        y = C.luma_padded(f, g.s, g.gw, g.gh)
        if st is None:
            st = C.ClassState.init(t)
            o, _, s = C.classify_first(st, t, y)
        else:
            o, st, s = C.classify_tiles(st, t, eq, y, (g.s, g.gw, g.gh))
        pw, px = pad, t["xh64"]
        out.append(o["cls"].copy())
        gmv.append(s.get("global_mv"))
        tiles.append(o.copy())
    return g, np.array(out), gmv, tiles


def truth_classes(g, cls, fields):
    """Class names found on the truth (frame, tile) pairs."""
    idx = {int(m): k for k, m in enumerate(g.mortons)}
    names = []
    for fid in fields:
        fr, m = FIELD.match(fid).groups()
        names.append(CLS[int(cls[int(fr), idx[int(m, 16)]])])
    return names


def non_truth_dirty(g, cls, fields):
    """Count of dirty (cls>=2) tile-frames not in the truth set (excluding frame 0)."""
    idx = {int(m): k for k, m in enumerate(g.mortons)}
    truth = {(int(a), idx[int(b, 16)]) for a, b in (FIELD.match(f).groups() for f in fields)}
    n = 0
    for fr in range(1, cls.shape[0]):
        for k in np.nonzero(cls[fr] >= 2)[0]:
            if (fr, int(k)) not in truth:
                n += 1
    return n


def build(spec):
    """Spec -> (frames, truth dict). Scenario bases may add animated rects before the op is applied."""
    from vadapt.synth import apply_op, render_scenario
    from vadapt.synth.synthetic import _animate
    b = spec["base"]
    frames, truth, _ = render_scenario(b["name"], b["seed"])
    if b.get("animate"):
        frames = [f.copy() for f in frames]
        for i, f in enumerate(frames):
            for r in b["animate"]:
                _animate(f, tuple(r), b["seed"], i)
    if spec["op"] == "scenario":
        return frames, truth
    frames, mut = apply_op(frames, spec["op"], tuple(spec["rect"]), spec["params"])
    return frames, mut["truth"]


def rect_tiles(g, rect):
    """Set of Morton ids covered by rect (closed box, via ids.field_ids_of_rect)."""
    from visioncore import ids
    return {int(FIELD.match(f).group(2), 16) for f in ids.field_ids_of_rect(tuple(rect), g.W, g.H, 0)}


def evaluate(spec, frames=None, truth=None):
    """-> metrics dict used by the assertions in test_px_mutants."""
    if frames is None:
        frames, truth = build(spec)
    g, cls, gmv, tiles = classify_frames(frames)
    ex = spec["expect"]
    m = {"grid": g, "cls": cls, "gmv": gmv, "tiles": tiles, "truth": truth,
         "dirty_total": int((cls >= 2).sum()), "frames": len(frames)}
    if spec.get("rect"):
        idx = {int(x): k for k, x in enumerate(g.mortons)}
        want = {CLS.index(c) for c in ex.get("classes", [])}
        inside = [idx[x] for x in rect_tiles(g, spec["rect"])]
        tf = {(int(a), idx[int(b, 16)]) for a, b in (FIELD.match(f).groups() for f in truth["fields"])}
        hits = [(fr, k) for fr, k in sorted(tf) if int(cls[fr, k]) in want]
        m["hits"] = len(hits)
        m["first_hit"] = min((fr for fr, _ in hits), default=None)
        oc = {CLS.index(c) for c in ex.get("outside_classes", [])}
        out_idx = np.ones(g.N, bool)
        out_idx[inside] = False
        m["outside"] = int(np.isin(cls[1:][:, out_idx], list(oc)).sum()) if oc else 0
    return m
