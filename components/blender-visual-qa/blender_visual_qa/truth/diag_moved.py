"""W10D lane D: MOVED-miss diagnosis on RIGID truth tiles (truth vs engine). Diagnostic only: it never writes a
profile, never feeds calibrate, and refuses GATE seeds (CAL = the W10b FIT arm, seeds 0..9).

Every RIGID tile (tl 3, non-zero truth mv) of frames t >= 1 gets ONE cause, first match wins:
  a  aperture-unobservable: the tile is reproduced by the zero vector AND by the true vector within the frame noise
     floor (any engine must say UNCHANGED);
  b  ambiguity: the true vector is one of the near-minimal SADs over the engine search window [-d, d]^2
     (d = min(s, MOTION_D_MAX)) and that set is not one compact basin (> 1 8-connected component, e.g. a 12 px
     checker) or is a plateau larger than 3x3 (1-D edge);
  c  boundary/occlusion: some tile pixel is occluded/disoccluded, on an object edge, or moves differently;
  d  out of search range (|mv| > d, source block outside the padded frame) or sub-pixel (median |frac| >= 1/4 px);
  e  genuine engine miss (sub-reason = the engine class of the tile).
Observable tiles = not a and not b; recall_obs = engine MOVED hits / observable tiles.
The engine is sealed: SAD here re-implements the engine convention cur(p) ~ prev(p - c) on the YCoCg-R luma plane.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

from . import pin as _pin  # the one engine pin, before any engine import

_pin.pin()
from visioncore import change, params as vparams, pipeline  # noqa: E402
from visioncore.grid import Grid  # noqa: E402

from . import labels  # noqa: E402

SCHEMA = "bvqa.moved-diag/v1"
CAUSES = ("a", "b", "c", "d", "e")
CAUSE_NAMES = {"a": "aperture", "b": "ambiguity", "c": "boundary_occlusion", "d": "range_subpix", "e": "engine_miss"}
CAL_SEEDS = range(0, 10)       # W10b FIT arm (fit:0-9); GATE 10..39 is never read here
NOISE_MIN_Q4 = 4               # 1/4 luma level per pixel: floor of the per-frame noise allowance
NOISE_PCT = 90                 # noise allowance = p90 of the zero-vector residual of STATIC tiles
BASIN_MAX = 9                  # near-minimum set larger than a 3x3 basin = plateau (1-D aperture)
TOL_DEN = 4                    # near-minimum: sad <= m + max(m // TOL_DEN, noise)
SUBPIX_Q4 = 4                  # 1/4 px
EXAMPLES = 3
TL_RIGID, TL_STATIC = labels.TL_ID["RIGID"], labels.TL_ID["STATIC"]
CLS_NAMES = ("UNCHANGED", "JITTER", "CHANGED", "MOVED", "FLICKER", "FROZEN", "DRIFT")


class DiagError(Exception):
    def __init__(self, code, msg, rc=2):
        super().__init__(msg)
        self.code, self.msg, self.rc = code, msg, rc


def search_d(s):
    return min(int(s), int(change.MOTION_D_MAX))


def luma(rgb, g):
    """Padded engine luma plane (int64) of an (H, W, 3) uint8 frame."""
    return change.luma_padded(np.ascontiguousarray(rgb), g.s, g.gw, g.gh).astype(np.int64)


def sad_grid(y_cur, y_prev, vec, g):
    """Per-tile SAD (gh, gw) of y_cur(p) vs y_prev(p - vec) and validity (whole source block inside the plane)."""
    dx, dy = int(vec[0]), int(vec[1])
    Hh, Ww = y_cur.shape
    sh = np.zeros((Hh, Ww), np.int64)
    ok = np.zeros((Hh, Ww), np.int64)
    ys0, ys1, xs0, xs1 = max(0, dy), min(Hh, Hh + dy), max(0, dx), min(Ww, Ww + dx)
    if ys0 < ys1 and xs0 < xs1:
        sh[ys0:ys1, xs0:xs1] = np.abs(y_cur[ys0:ys1, xs0:xs1] - y_prev[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx])
        ok[ys0:ys1, xs0:xs1] = 1
    s = g.s
    sad = sh.reshape(g.gh, s, g.gw, s).sum(axis=(1, 3))
    valid = ok.reshape(g.gh, s, g.gw, s).sum(axis=(1, 3)) == s * s
    return sad, valid


def sad_window(y_cur, y_prev, g, d):
    """-> sad int64 (2d+1, 2d+1, gh, gw) indexed [dy+d, dx+d]; invalid source blocks are int64 max."""
    big = np.iinfo(np.int64).max
    k = 2 * d + 1
    out = np.full((k, k, g.gh, g.gw), big, np.int64)
    for dy in range(-d, d + 1):
        for dx in range(-d, d + 1):
            sad, valid = sad_grid(y_cur, y_prev, (dx, dy), g)
            out[dy + d, dx + d] = np.where(valid, sad, big)
    return out


def _components(mask):
    """8-connected components of a small bool 2-D mask."""
    seen = np.zeros(mask.shape, bool)
    n = 0
    h, w = mask.shape
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        n += 1
        seen[y0, x0] = True
        stack = [(int(y0), int(x0))]
        while stack:
            y, x = stack.pop()
            for yy in range(max(0, y - 1), min(h, y + 2)):
                for xx in range(max(0, x - 1), min(w, x + 2)):
                    if mask[yy, xx] and not seen[yy, xx]:
                        seen[yy, xx] = True
                        stack.append((yy, xx))
    return n


def ambiguous(win, noise, tmv, d):
    """win: (2d+1, 2d+1) SAD of one tile; tmv: true vector. Ambiguous = the true vector is inside the window and one
    of the near-minima, and the near-minimum set is not one compact basin. -> (is_ambiguous, n_components, n_near)."""
    big = np.iinfo(np.int64).max
    m = int(win.min())
    if m == big:
        return False, 0, 0
    near = win <= m + max(m // TOL_DEN, int(noise))
    nc, nn = _components(near), int(near.sum())
    inside = max(abs(tmv[0]), abs(tmv[1])) <= d and bool(near[tmv[1] + d, tmv[0] + d])
    return inside and (nc > 1 or nn > BASIN_MAX), nc, nn


def noise_floor(sad0, tl_rm, nn):
    """Per-frame noise allowance in SAD units: p90 of the zero-vector SAD of STATIC tiles, >= NOISE_MIN_Q4 per px."""
    st = sad0[tl_rm == TL_STATIC]
    q = int(np.percentile(st, NOISE_PCT)) if st.size else 0
    return max(q, nn * NOISE_MIN_Q4 // 16)


def diagnose_frame(prev_rgb, cur_rgb, prev_truth, cur_truth, lab, cls, mv_pred, g, frame=0, ring=labels.EDGE_RING):
    """One frame pair. lab: truth label dict (Morton order: tl, mv); cls/mv_pred: engine Morton arrays (N,), (N, 2).
    -> list of per-tile dicts for every RIGID tile with non-zero truth mv."""
    tl = np.asarray(lab["tl"])
    tmv_all = np.asarray(lab["mv"]).astype(np.int64)
    rig = np.flatnonzero((tl == TL_RIGID) & (np.abs(tmv_all).max(axis=-1) > 0))
    if rig.size == 0:
        return []
    d, s, nn = search_d(g.s), g.s, g.s * g.s
    yc, yp = luma(cur_rgb, g), luma(prev_rgb, g)
    win = sad_window(yc, yp, g, d)
    sad0 = win[d, d]
    tl_rm = np.empty(g.N, np.int64)
    tl_rm[g.flat] = tl
    noise = noise_floor(sad0, tl_rm.reshape(g.gh, g.gw), nn)
    pp = labels.pixel_predicates(prev_truth, cur_truth, ring=ring)
    mvq = cur_truth["mv"].astype(np.int64)
    frac = np.abs(mvq - 16 * labels.round_px(cur_truth["mv"]).astype(np.int64)).max(axis=2)
    out = []
    for i in rig:
        tx, ty = int(g.tx[i]), int(g.ty[i])
        tmv = (int(tmv_all[i, 0]), int(tmv_all[i, 1]))
        ys, xs = slice(ty * s, min((ty + 1) * s, g.H)), slice(tx * s, min((tx + 1) * s, g.W))
        st_all, ok_all = sad_grid(yc, yp, tmv, g)
        sad_t, ok_t = int(st_all[ty, tx]), bool(ok_all[ty, tx])
        s0 = int(sad0[ty, tx])
        amb, ncomp, nnear = ambiguous(win[:, :, ty, tx], noise, tmv, d)
        mvpx = pp["mvpx"][ys, xs]
        mixed = bool(((mvpx[..., 0] != tmv[0]) | (mvpx[..., 1] != tmv[1])).any())
        boundary = bool(pp["occ"][ys, xs].any() or pp["edge"][ys, xs].any() or mixed)
        out_range = max(abs(tmv[0]), abs(tmv[1])) > d or not ok_t
        subpix = int(np.median(frac[ys, xs])) >= SUBPIX_Q4
        if s0 <= noise and sad_t <= noise:
            cause = "a"
        elif amb:
            cause = "b"
        elif boundary:
            cause = "c"
        elif out_range or subpix:
            cause = "d"
        else:
            cause = "e"
        c = int(cls[i])
        out.append({"frame": int(frame), "tile": int(i), "tx": tx, "ty": ty, "mv": list(tmv), "cause": cause,
                    "hit": c == change.C_MOVED, "mv_ok": c == change.C_MOVED and (int(mv_pred[i][0]), int(mv_pred[i][1])) == tmv,
                    "cls": CLS_NAMES[c], "sad0": s0, "sad_t": sad_t, "noise": int(noise), "near_comp": ncomp,
                    "near_n": nnear, "out_range": bool(out_range), "subpix": bool(subpix)})
    return out


def observable_mask(tiles, n_tiles):
    """Truth-amendment helper: bool[N] (Morton) False exactly on RIGID tiles of cause a/b; the proposed truth flag
    FLAG_UNOBSERVABLE is its complement on RIGID tiles."""
    m = np.ones(int(n_tiles), bool)
    for t in tiles:
        if t["cause"] in ("a", "b"):
            m[t["tile"]] = False
    return m


def summarize(tiles):
    """-> counts/ppm per cause, observable recall, engine sub-reasons, 3 examples per cause (misses first)."""
    n = len(tiles)
    miss = [t for t in tiles if not t["hit"]]
    per = {}
    for c in CAUSES:
        sel = [t for t in tiles if t["cause"] == c]
        ms = [t for t in sel if not t["hit"]]
        ex = _examples(ms + [t for t in sel if t["hit"]])
        per[c] = {"name": CAUSE_NAMES[c], "n": len(sel), "hit": len(sel) - len(ms), "miss": len(ms),
                  "mv_exact": sum(t["mv_ok"] for t in sel), "ppm_of_rigid": labels_ppm(len(sel), n),
                  "ppm_of_miss": labels_ppm(len(ms), len(miss)),
                  "examples": [{k: t[k] for k in ("bench", "seed", "frame", "tile", "tx", "ty", "mv", "cls", "hit")
                                if k in t} for t in ex]}
    obs = [t for t in tiles if t["cause"] not in ("a", "b")]
    eng = [t for t in tiles if t["cause"] == "e"]
    per_b = [t for t in tiles if t["cause"] == "b" and t["near_comp"] > 1]
    pla_b = [t for t in tiles if t["cause"] == "b" and not t["near_comp"] > 1]
    obs2 = obs + pla_b  # observable if a 1-D plateau counts as resolvable (global/island vectors can break it)
    sub = {}
    for t in eng:
        if not t["hit"]:
            sub[t["cls"]] = sub.get(t["cls"], 0) + 1
    return {"schema": SCHEMA, "rigid_n": n, "hit": n - len(miss), "miss": len(miss),
            "recall_ppm": labels_ppm(n - len(miss), n), "mv_exact": sum(t["mv_ok"] for t in tiles),
            "observable_n": len(obs), "observable_hit": sum(t["hit"] for t in obs),
            "recall_obs_ppm": labels_ppm(sum(t["hit"] for t in obs), len(obs)),
            "mv_exact_obs_ppm": labels_ppm(sum(t["mv_ok"] for t in obs), len(obs)),
            "engine_addressable_n": len(eng), "recall_e_ppm": labels_ppm(sum(t["hit"] for t in eng), len(eng)),
            "engine_miss_by_cls": dict(sorted(sub.items())), "causes": per,
            "b_split": {"periodic": {"n": len(per_b), "hit": sum(t["hit"] for t in per_b)},
                        "plateau": {"n": len(pla_b), "hit": sum(t["hit"] for t in pla_b)}},
            "observable2_n": len(obs2), "recall_obs2_ppm": labels_ppm(sum(t["hit"] for t in obs2), len(obs2))}


def _examples(ts):
    """Up to EXAMPLES tiles, one per (bench, frame) first so the examples are not one frame repeated."""
    out, seen = [], set()
    for t in ts:
        key = (t.get("bench"), t["frame"])
        if key not in seen:
            seen.add(key)
            out.append(t)
    for t in ts:
        if len(out) >= EXAMPLES:
            break
        if t not in out:
            out.append(t)
    return out[:EXAMPLES]


def labels_ppm(num, den):
    return None if not den else int(num) * 1000000 // int(den)


# --- engine drive -----------------------------------------------------------------------------------------------
def engine_run(rgbs, table=None):
    """Sealed engine on in-memory frames -> (Grid, cls (F, N), mv (F, N, 2)) in Morton order."""
    from visioncore import frame as vframe
    H, W = rgbs[0].shape[:2]
    frames = [vframe.Frame(i, i * 41667, i, np.ascontiguousarray(r)) for i, r in enumerate(rgbs)]
    src = vframe.MemoryFrameSource(frames, {"width": W, "height": H, "count": len(frames)})
    vrun = pipeline.analyze(src, dict(vparams.effective(None)) if table is None else table)
    cls = np.stack([t["cls"] for t in vrun.tiles]).astype(np.int64)
    mv = np.stack([np.stack([t["mv_dx"], t["mv_dy"]], axis=-1) for t in vrun.tiles]).astype(np.int64)
    return vrun.grid, cls, mv


def diagnose_sequence(rgbs, truths, cls, mv, g, tag=None, label_list=None):
    """rgbs/truths: per-frame beauty and truth dicts (ob, mv, z); label_list: truth labels per frame (default:
    labels.label_sequence without journal). -> per-tile list over frames 1.."""
    labs = label_list if label_list is not None else labels.label_sequence(truths, g)
    out = []
    for f in range(1, len(rgbs)):
        for t in diagnose_frame(rgbs[f - 1], rgbs[f], truths[f - 1], truths[f], labs[f], cls[f], mv[f], g, frame=f):
            if tag:
                t.update(tag)
            out.append(t)
    return out


# --- CAL-seed Blender driver (frames stay in the RAM workspace) ---------------------------------------------------
def _cal_seeds(text):
    lo, _, hi = text.partition("-")
    seeds = list(range(int(lo), int(hi or lo) + 1))
    if not seeds or any(k not in CAL_SEEDS for k in seeds):
        raise DiagError("E_GATE", "seeds outside the CAL range %d-%d (GATE seeds are never diagnosed)"
                        % (CAL_SEEDS[0], CAL_SEEDS[-1]), rc=5)
    return seeds


def diagnose_unit(run_bench, unit, bench, seed, table):
    """Analyze + label one rendered unit exactly as run_bench.analyze_bench does, then diagnose it."""
    vrun, _ = run_bench.vision_analyze(unit, table)
    resid = run_bench.raw_resid(vrun, table)
    label_resid = None if bench in run_bench.CLEAN_BENCHES else resid
    tt, lres, _ = run_bench.truth_labels(unit, label_resid, {"T0_Q4": int(table["T0_Q4"])})
    g = vrun.grid
    cls = np.stack([t["cls"] for t in vrun.tiles]).astype(np.int64)
    mv = np.stack([np.stack([t["mv_dx"], t["mv_dy"]], axis=-1) for t in vrun.tiles]).astype(np.int64)
    rgbs = [f.rgb for f in pipeline.ManifestSource(Path(unit) / "vision_in" / "frames.manifest.json")]
    truths = [labels.load_frame(str(Path(unit) / "truth"), ri) for ri in lres["frame_ids"]]
    if not len(rgbs) == len(truths) == tt["tl"].shape[0] == cls.shape[0]:
        raise DiagError("E_STALE", "frame count mismatch", rc=4)
    labs = [{"tl": tt["tl"][f], "mv": tt["mv"][f]} for f in range(len(rgbs))]
    return diagnose_sequence(rgbs, truths, cls, mv, g, {"bench": bench, "seed": seed}, labs)


def run_cal(benches, seeds, nframes, w, h, spp, campaign="w10d", quota_mb=1024):
    from vadapt import ramws
    from . import run_bench
    run_id = "d%d-%x" % (int(time.time()) % 100000, time.process_time_ns() & 0xFFFF)
    ws = ramws.open_run(campaign, run_id, quota_mb << 20, tool=run_bench.RAM_TOOL)
    run_bench._Store.ws = ws
    table = dict(vparams.effective(None))
    tiles, fails = [], []
    try:
        for k in seeds:
            for b in benches:
                unit = ws.path("diag/%s/seed_%06d/" % (b, k))
                try:
                    run_bench.render_bench(b, unit, w, h, nframes, spp, k)
                    tiles += diagnose_unit(run_bench, unit, b, k, table)
                except Exception as e:  # one failed unit is reported, the diagnosis of the others continues
                    fails.append({"bench": b, "seed": k, "error": "%s: %s" % (type(e).__name__, str(e)[:160])})
                finally:
                    if Path(unit).exists():
                        ws.release(str(unit))
    finally:
        run_bench._Store.ws = None
        ws.close()
    return tiles, fails


def _report(tiles, fails, run):
    by_bench = {b: summarize([t for t in tiles if t["bench"] == b]) for b in run["benches"]}
    for v in by_bench.values():
        for c in v["causes"].values():
            c.pop("examples")
    return {"summary": summarize(tiles), "by_bench": by_bench, "failures": fails, "tiles": tiles, "run": run}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="blender_visual_qa.truth.diag_moved")
    ap.add_argument("--out", required=True, help="JSON summary path (no frames are ever written)")
    ap.add_argument("--benches", default="B1,B1x,B2,B4,B5o,B7,BS")
    ap.add_argument("--seeds", default="0-1", help="CAL seed range lo-hi within 0-9")
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--res", default="320x180")
    ap.add_argument("--spp", type=int, default=4)
    ap.add_argument("--resummarize", default=None, help="re-derive the summary from the per-tile records of a JSON")
    a = ap.parse_args(argv)
    if a.resummarize:
        old = json.loads(Path(a.resummarize).read_text())
        tiles, fails, run = old["tiles"], old["failures"], old["run"]
        Path(a.out).write_text(json.dumps(_report(tiles, fails, run), sort_keys=True, indent=1) + "\n")
        return 0
    try:
        seeds = _cal_seeds(a.seeds)
    except (DiagError, ValueError) as e:
        sys.stdout.write(json.dumps({"code": getattr(e, "code", "E_ARGS"), "rc": getattr(e, "rc", 2)}) + "\n")
        return getattr(e, "rc", 2)
    w, h = (int(v) for v in a.res.split("x"))
    benches = [b for b in a.benches.split(",") if b]
    tiles, fails = run_cal(benches, seeds, a.frames, w, h, a.spp)
    rep = _report(tiles, fails, {"benches": benches, "seeds": seeds, "frames": a.frames, "res": [w, h], "spp": a.spp})
    summ = rep["summary"]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, sort_keys=True, indent=1) + "\n")
    sys.stdout.write(json.dumps({"code": "OK" if not fails else "PARTIAL", "rigid_n": summ["rigid_n"],
                                 "recall_ppm": summ["recall_ppm"], "recall_obs_ppm": summ["recall_obs_ppm"]}) + "\n")
    return 0 if not fails else 3


if __name__ == "__main__":
    sys.exit(main())
