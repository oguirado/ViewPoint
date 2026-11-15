"""Join visioncore tile predictions with Blender ground-truth tile labels -> bvqa.calib-row/v1 (BDESIGN_TRUTH 2.4/2.7).

Host side, numpy only. Integers only (ppm); visioncore is imported read-only for canon/errors/layout.
Units: mv_truth and mv_pred are integer PIXELS per tile, shape (F, N, 2) = (dx, dy), image coords (row-down).
Class ids are visioncore CLS ids: UNCHANGED 0, JITTER 1, CHANGED 2, MOVED 3, FLICKER 4, FROZEN 5, DRIFT 6.
"""
import json
import struct
import sys
from pathlib import Path

import numpy as np

sys.dont_write_bytecode = True


from . import pin as _pin  # noqa: E402  JW9c-1/2: the one engine pin, before any engine import

ENGINE_WHEEL = _pin.pin()[0]
from visioncore import canon  # noqa: E402
from visioncore.errors import VqaError  # noqa: E402

SCHEMA = "bvqa.calib-row/v1"
C_UNCHANGED, C_JITTER, C_CHANGED, C_MOVED, C_FLICKER, C_FROZEN, C_DRIFT = range(7)
DIRTY_MIN = 2
TL_MAX = 11
TL_FLICKER, TL_FROZEN, TL_DRIFT = 9, 10, 11
EXCLUDED = (1, 2, 8)
# tl -> (kind, accepted predicted classes). kind "clean": must NOT be dirty; "dirty": must be dirty.
EXPECT = {
    0: ("clean", (C_UNCHANGED, C_JITTER)),
    3: ("dirty", (C_MOVED,)),
    4: ("dirty", (C_CHANGED, C_MOVED)),
    5: ("dirty", (C_CHANGED, C_MOVED)),
    6: ("dirty", (C_CHANGED,)),
    7: ("clean", (C_UNCHANGED, C_JITTER)),
    9: ("dirty", (C_FLICKER,)),
    10: ("dirty", (C_FROZEN,)),
    11: ("dirty", (C_DRIFT, C_CHANGED)),
}
FP_POP = (0, 7)  # tiles where a dirty verdict is a false positive for the T0 fit (STATIC/SHADE_WEAK); SUBPIX (2) is
# real sub-pixel motion, scored as motion-tolerant and never as FP evidence (DIAG_DNOI RC4)
TL_NAMES = ("STATIC", "INVISIBLE", "SUBPIX", "RIGID", "OCCLUDE", "DEFORM", "SHADE_STRONG",
            "SHADE_WEAK", "SHADE_BAND", "FLICKER_T", "FROZEN_T", "DRIFT_T")
# FROZEN_T hit strata by tile coverage (BTRUTH 5.1): name -> (lo ppm inclusive, hi ppm exclusive); 150k..300k is unreported
COV_STRATA = (("lt50k", 0, 50000), ("50k_150k", 50000, 150000), ("ge300k", 300000, 1000001))
MV_HIST = 17  # L-inf error 0..15, 16 = >=16 px
TRUTH_MAGIC = b"BVQTRUT1"
TRUTH_HDR_FMT = "<8sHHHHHHHHII16s16s"
TRUTH_HDR_SIZE = 64
FLAG_UNOBSERVABLE = 8  # labels.FLAG_UNOBSERVABLE (W10F): RIGID tile unobservable from beauty + truth (diag_moved a/b)
TRUTH_REC = np.dtype([("tl", "u1"), ("flags", "u1"), ("dx", "i1"), ("dy", "i1"),
                      ("ob", "<u2"), ("occ", "<u2")])


def ppm(num, den):
    return None if den == 0 else (int(num) * 1000000) // int(den)


def read_truth_tiles(path, expect_grid=None):
    """bvqa.truth-tile/v1 -> dict(tl, flags, mv[F,N,2], ob, occ, hdr). expect_grid=(s,W,H,gw,gh,N); mismatch -> E_STALE rc 4."""
    b = Path(path).read_bytes()
    if len(b) < TRUTH_HDR_SIZE:
        raise VqaError("E_SCHEMA", {"reason": "short truth-tile header"})
    f = struct.unpack(TRUTH_HDR_FMT, b[:TRUTH_HDR_SIZE])
    if f[0] != TRUTH_MAGIC or f[1] != 1:
        raise VqaError("E_SCHEMA", {"reason": "bad magic/version"})
    hdr = {"s": f[2], "W": f[3], "H": f[4], "gw": f[5], "gh": f[6], "N": f[7], "n_frames": f[9], "f0": f[10]}
    if expect_grid is not None and tuple(expect_grid) != (hdr["s"], hdr["W"], hdr["H"], hdr["gw"], hdr["gh"], hdr["N"]):
        raise VqaError("E_STALE", {"reason": "grid ident mismatch", "truth": [hdr["s"], hdr["W"], hdr["H"]]})
    n = hdr["n_frames"] * hdr["N"]
    if len(b) != TRUTH_HDR_SIZE + 8 * n:
        raise VqaError("E_SCHEMA", {"reason": "payload size", "have": len(b)})
    rec = np.frombuffer(b, dtype=TRUTH_REC, count=n, offset=TRUTH_HDR_SIZE).reshape(hdr["n_frames"], hdr["N"])
    if n and int(rec["tl"].max()) > TL_MAX:
        raise VqaError("E_SCHEMA", {"reason": "tl out of range"})
    mv = np.stack([rec["dx"], rec["dy"]], axis=-1)
    return {"tl": rec["tl"].copy(), "flags": rec["flags"].copy(), "mv": mv, "ob": rec["ob"].copy(),
            "occ": rec["occ"].copy(), "hdr": hdr}


def _drift_duty(tl, dirty):
    """DRIFT_T tiles: share of dirty verdicts at/after each tile's first detection -> (n, hit)."""
    m = tl == TL_DRIFT
    if not m.any():
        return 0, 0
    hit_any = m & dirty
    first = np.where(hit_any.any(axis=0), hit_any.argmax(axis=0), tl.shape[0])  # per tile column
    after = m & (np.arange(tl.shape[0])[:, None] >= first[None, :])
    return int(after.sum()), int((after & dirty).sum())


def _onset_latency(truth_on, pred_on):
    """Max over tiles of frames from the first truth-active frame to the first predicted one (never predicted ->
    frames in the sequence). None when no tile is truth-active."""
    if not truth_on.any():
        return None
    nf, worst = truth_on.shape[0], 0
    for j in np.flatnonzero(truth_on.any(axis=0)):
        onset = int(np.argmax(truth_on[:, j]))
        hit = np.flatnonzero(pred_on[onset:, j])
        worst = max(worst, int(hit[0]) if hit.size else nf)
    return worst


def score_frames(tl, cls_pred, mv_truth, mv_pred, bench, seed, spp, grid, t0_q4, resid=None, mv_sign=1, cov=None,
                 flags=None):
    """Return one bvqa.calib-row/v1 dict. tl/cls_pred (F,N); mv_* (F,N,2); resid (F,N) optional q4 S1 residual;
    cov (F,N) optional tile coverage ppm used to stratify FROZEN_T hits. flags (F,N) optional truth flags: RIGID tiles
    with FLAG_UNOBSERVABLE leave the MOVED denominators (tl "3", mv_n, mv_exact) and the unmasked counts are reported
    as mv_n_unobs / mv_n_raw / mv_exact_raw / moved_raw. flags=None -> the row is exactly the pre-W10F row.

    mv_sign=-1 reproduces mutant bt.mvsign.01 (scorer negates predicted mv)."""
    tl, cls_pred = np.asarray(tl), np.asarray(cls_pred)
    mv_truth, mv_pred = np.asarray(mv_truth), np.asarray(mv_pred)
    if tl.shape != cls_pred.shape or tl.ndim != 2:
        raise VqaError("E_SCHEMA", {"reason": "tl/cls shape", "tl": list(tl.shape), "cls": list(cls_pred.shape)})
    if mv_truth.shape != tl.shape + (2,) or mv_pred.shape != mv_truth.shape:
        raise VqaError("E_SCHEMA", {"reason": "mv shape"})
    if tl.size and (int(tl.max()) > TL_MAX or int(tl.min()) < 0):
        raise VqaError("E_SCHEMA", {"reason": "tl out of range"})
    if cls_pred.size and (int(cls_pred.max()) > C_DRIFT or int(cls_pred.min()) < 0):
        raise VqaError("E_SCHEMA", {"reason": "cls out of range"})
    rig_raw = (tl == 3) & (np.abs(mv_truth).max(axis=-1) > 0)
    unobs = None
    if flags is not None:
        flags = np.asarray(flags)
        if flags.shape != tl.shape:
            raise VqaError("E_SCHEMA", {"reason": "flags shape"})
        unobs = rig_raw & ((flags.astype(np.int64) & FLAG_UNOBSERVABLE) != 0)
    dirty = cls_pred >= DIRTY_MIN
    per = {}
    for t in sorted(EXPECT):
        m = tl == t
        if t == 3 and unobs is not None:
            m = m & ~unobs
        n = int(m.sum())
        if n == 0:
            continue
        kind, ok_cls = EXPECT[t]
        ok = np.isin(cls_pred[m], ok_cls)
        per[str(t)] = {"n": n, "dirty": int(dirty[m].sum()), "cls_ok": int(ok.sum())}
    # motion: RIGID tiles with non-zero truth mv
    rig = rig_raw if unobs is None else rig_raw & ~unobs
    mvp = mv_pred.astype(np.int64) * int(mv_sign)
    moved = rig & (cls_pred == C_MOVED)
    err = np.abs(mvp[moved] - mv_truth[moved].astype(np.int64)).max(axis=-1) if moved.any() else np.zeros(0, np.int64)
    hist = np.bincount(np.minimum(err, MV_HIST - 1), minlength=MV_HIST).tolist()
    row = {
        "schema": SCHEMA, "bench": bench, "seed": int(seed), "spp": int(spp), "grid": grid, "t0_q4": int(t0_q4),
        "frames": int(tl.shape[0]), "tiles": int(tl.shape[1]),
        "excluded": int(np.isin(tl, EXCLUDED).sum()),
        "tl": per,
        "mv_n": int(rig.sum()),
        "mv_exact": int((moved & (mvp == mv_truth).all(axis=-1)).sum()),
        "mv_err_hist": [int(x) for x in hist],
    }
    if unobs is not None:
        moved_raw = rig_raw & (cls_pred == C_MOVED)
        row["mv_n_unobs"] = int(unobs.sum())
        row["mv_n_raw"] = int(rig_raw.sum())
        row["mv_exact_raw"] = int((moved_raw & (mvp == mv_truth).all(axis=-1)).sum())
        row["moved_raw"] = {"n": int((tl == 3).sum()), "cls_ok": int(((tl == 3) & (cls_pred == C_MOVED)).sum())}
    dn, dh = _drift_duty(tl, dirty)
    row["drift_duty_n"], row["drift_duty_hit"] = dn, dh
    row["drift_duty_ppm"] = ppm(dh, dn) if dn else 0
    frozen = tl == TL_FROZEN
    if cov is not None:
        cov = np.asarray(cov)
        if cov.shape != tl.shape:
            raise VqaError("E_SCHEMA", {"reason": "cov shape"})
    fh = {}
    for name, lo, hi in COV_STRATA:
        sel = frozen & (cov >= lo) & (cov < hi) if cov is not None else np.zeros(tl.shape, bool)
        fh[name] = {"n": int(sel.sum()), "hit": int((sel & (cls_pred == C_FROZEN)).sum())}
    row["frozen_hit"] = fh
    # flicker verdicts on tiles that are not FLICKER_T and not excluded (B4n control: must be 0)
    row["flicker_fp_tiles"] = int(((cls_pred == C_FLICKER) & (tl != TL_FLICKER) & ~np.isin(tl, EXCLUDED)).sum())
    row["drift_fp_tiles"] = int(((cls_pred == C_DRIFT) & (tl != TL_DRIFT) & ~np.isin(tl, EXCLUDED)).sum())
    lat = _onset_latency(tl == TL_FLICKER, cls_pred == C_FLICKER)
    if lat is not None:
        row["latency_max"] = {"9": lat}
    if resid is not None:
        resid = np.asarray(resid)
        if resid.shape != tl.shape:
            raise VqaError("E_SCHEMA", {"reason": "resid shape"})
        pop = np.isin(tl, FP_POP)
        row["fp_pop_n"] = int(pop.sum())
        row["fp_resid_max_q4"] = int(resid[pop].max()) if pop.any() else 0
        row["fp_resid_by_tl"] = {str(t): int(resid[tl == t].max()) for t in FP_POP if (tl == t).any()}
    return row


SELECT_BUDGETS = (8, 16, 32)  # C5: select(B) budgets (BTRUTH 6)
DIRTY_TL = tuple(t for t, (k, _) in sorted(EXPECT.items()) if k == "dirty")


def c1b_flips(tl, cls_pred):
    """C1b (B6 still): dirty/clean verdict toggles between consecutive frames on non-excluded tiles (frames >= 1)."""
    tl, cls_pred = np.asarray(tl), np.asarray(cls_pred)
    if tl.shape[0] < 2:
        return 0
    dirty = cls_pred >= DIRTY_MIN
    keep = ~np.isin(tl[1:], EXCLUDED) & ~np.isin(tl[:-1], EXCLUDED)
    return int(((dirty[1:] != dirty[:-1]) & keep).sum())


def c5_select(tiles_seq, grid, tl, t0_q4, budgets=SELECT_BUDGETS):
    """C5: every truth-dirty tile with engine resid > R_SEL lies under a node emitted by the engine's own
    Pyramid.select(B) (frames >= 1, every B in budgets) -> {"n", "hit"}. visioncore.pyramid is resolved through
    sys.path at call time, so the selector that runs is the engine on the path (never a local copy)."""
    from visioncore import params as vparams, pyramid
    rsel = pyramid.r_sel(vparams.effective({"T0_Q4": int(t0_q4)}))
    tl = np.asarray(tl)
    n = hit = 0
    for f in range(1, len(tiles_seq)):
        tiles = tiles_seq[f]
        need = np.isin(tl[f], DIRTY_TL) & (np.asarray(tiles["resid"]).astype(np.int64) > rsel)
        if not need.any():
            continue
        pyr = pyramid.Pyramid(tiles, grid)
        m = np.asarray(tiles["morton"]).astype(np.int64)
        for b in budgets:
            cov = np.zeros(4 ** pyr.L, dtype=bool)
            for level, m0, _, _ in pyr.select(int(b), rsel):
                cov[m0:m0 + 4 ** level] = True
            n += int(need.sum())
            hit += int((need & cov[m]).sum())
    return {"n": n, "hit": hit}


def _mode_mv(mv):
    """Most frequent (dx, dy) row; ties -> lexicographically smallest (deterministic)."""
    vals, cnt = np.unique(mv.reshape(-1, 2).astype(np.int64), axis=0, return_counts=True)
    i = int(np.flatnonzero(cnt == cnt.max())[0])
    return int(vals[i][0]), int(vals[i][1])


def b5o_global_mv(tl, mv_truth, global_mv):
    """B5o: engine per-frame global_mv equals the truth camera motion = mode of the truth tile mv over non-excluded
    tiles (frames >= 1) -> {"n", "exact"}. Same image-coordinate convention as the tile mv (mv_sign 1)."""
    tl, mv_truth = np.asarray(tl), np.asarray(mv_truth)
    n = ex = 0
    for f in range(1, tl.shape[0]):
        keep = ~np.isin(tl[f], EXCLUDED)
        if not keep.any() or f >= len(global_mv):
            continue
        g = global_mv[f]
        n += 1
        ex += int(_mode_mv(mv_truth[f][keep]) == (int(g[0]), int(g[1])))
    return {"n": n, "exact": ex}


def s4_iou(tl, ob, morton, regions_seq):
    """C6 / S4: per (frame >= 1, truth object ob != 0) the best tile IoU (ppm) of its truth-dirty tiles against the
    engine regions of that frame -> sorted list of ppm samples (empty: no truth-dirty object)."""
    from visioncore import regions as vreg
    tl, ob, morton = np.asarray(tl), np.asarray(ob), np.asarray(morton).astype(np.int64)
    out = []
    for f in range(1, tl.shape[0]):
        regs = [set(vreg.region_tiles(r)) for r in (regions_seq[f] if f < len(regions_seq) else [])]
        dirty = np.isin(tl[f], DIRTY_TL)
        for o in sorted(set(int(x) for x in ob[f][dirty]) - {0}):
            t = set(int(x) for x in morton[dirty & (ob[f] == o)])
            out.append(max([len(t & r) * 1000000 // len(t | r) for r in regs], default=0))
    return sorted(out)


def summarize(rows):
    """Aggregate rows -> ppm metrics (integers or None when the population is absent)."""
    tot = {}
    mv_n = mv_exact = 0
    hist = [0] * MV_HIST
    fp_max = 0
    fp_n = 0
    dn = dh = 0
    fz = {name: [0, 0] for name, _, _ in COV_STRATA}
    flick = None
    obs = any("mv_n_unobs" in r for r in rows)
    raw = {"mv_n_unobs": 0, "mv_n_raw": 0, "mv_exact_raw": 0, "n": 0, "cls_ok": 0}
    for r in rows:
        if r.get("schema") != SCHEMA:
            raise VqaError("E_SCHEMA", {"reason": "not a calib row"})
        for t, d in r["tl"].items():
            a = tot.setdefault(t, {"n": 0, "dirty": 0, "cls_ok": 0})
            for k in a:
                a[k] += d[k]
        mv_n += r["mv_n"]
        mv_exact += r["mv_exact"]
        if obs:  # rows without flags count as fully observable (raw == observable)
            t3 = r["tl"].get("3", {"n": 0, "cls_ok": 0})
            mr = r.get("moved_raw", {"n": t3["n"], "cls_ok": t3["cls_ok"]})
            raw["mv_n_unobs"] += r.get("mv_n_unobs", 0)
            raw["mv_n_raw"] += r.get("mv_n_raw", r["mv_n"])
            raw["mv_exact_raw"] += r.get("mv_exact_raw", r["mv_exact"])
            raw["n"] += mr["n"]
            raw["cls_ok"] += mr["cls_ok"]
        for i, v in enumerate(r["mv_err_hist"]):
            hist[i] += v
        fp_max = max(fp_max, r.get("fp_resid_max_q4", 0))
        fp_n += r.get("fp_pop_n", 0)
        dn += r.get("drift_duty_n", 0)
        dh += r.get("drift_duty_hit", 0)
        for name, v in r.get("frozen_hit", {}).items():
            fz[name][0] += v["n"]
            fz[name][1] += v["hit"]
        if str(r.get("bench", "")).startswith("B4n"):
            flick = (flick or 0) + r.get("flicker_fp_tiles", 0)
    out = {"tl": {}, "mv_n": mv_n, "fp_resid_max_q4": fp_max, "fp_pop_n": fp_n,
           "drift_duty_ppm": ppm(dh, dn), "flicker_fp_tiles": flick,
           "frozen_hit": {k: {"n": v[0], "hit": v[1], "ppm": ppm(v[1], v[0])} for k, v in fz.items()}}
    for t, a in sorted(tot.items(), key=lambda kv: int(kv[0])):
        kind = EXPECT[int(t)][0]
        m = {"n": a["n"], "dirty_ppm": ppm(a["dirty"], a["n"]), "cls_ok_ppm": ppm(a["cls_ok"], a["n"]), "fp": 0}
        if kind == "clean":
            m["fp"] = a["dirty"]
        out["tl"][t] = m
    out["mv_exact_ppm"] = ppm(mv_exact, mv_n)
    nm = sum(hist)
    p95 = None
    if nm:
        c = 0
        for e, v in enumerate(hist):
            c += v
            if c * 100 >= 95 * nm:
                p95 = e
                break
    out["mv_err_p95_px"] = p95
    out["mv_moved_n"] = nm
    if obs:  # W10F: observable recall is tl "3" cls_ok_ppm / mv_exact_ppm; the unmasked recall is reported alongside
        out["mv_n_unobs"], out["mv_n_raw"] = raw["mv_n_unobs"], raw["mv_n_raw"]
        out["mv_exact_raw_ppm"] = ppm(raw["mv_exact_raw"], raw["mv_n_raw"])
        out["moved_recall_raw_ppm"] = ppm(raw["cls_ok"], raw["n"])
        out["moved_recall_obs_ppm"] = (out["tl"].get("3") or {}).get("cls_ok_ppm")
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="blender_visual_qa.truth.score")
    ap.add_argument("--labels", required=True, help="bvqa.truth-tile/v1 file")
    ap.add_argument("--pred", required=True, help=".npz with cls[F,N] u8, mv[F,N,2] i (px), optional resid[F,N]")
    ap.add_argument("--out", required=True, help="rows.jsonl (appended)")
    ap.add_argument("--bench", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--spp", type=int, required=True)
    ap.add_argument("--grid", required=True)
    ap.add_argument("--t0", type=int, required=True)
    ap.add_argument("--observable", action="store_true", help="drop FLAG_UNOBSERVABLE tiles from the MOVED denominators")
    a = ap.parse_args(argv)
    try:
        lab = read_truth_tiles(a.labels)
        p = np.load(a.pred)
        row = score_frames(lab["tl"], p["cls"], lab["mv"], p["mv"], a.bench, a.seed, a.spp, a.grid, a.t0,
                           resid=p["resid"] if "resid" in p.files else None,
                           cov=p["cov"] if "cov" in p.files else None,
                           flags=lab["flags"] if a.observable else None)
        with open(a.out, "a", encoding="utf-8") as fh:
            fh.write(canon.dumps(row))
        return 0
    except VqaError as e:
        sys.stdout.write(json.dumps(e.to_obj(), sort_keys=True) + "\n")
        return e.rc


if __name__ == "__main__":
    sys.exit(main())
