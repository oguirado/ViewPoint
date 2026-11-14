"""X3 - PX detection power: 5 pixel mutants x 3 levels x N seeds (default 30) on synthetic clean captures.
Gate G-E3 (SPEC_INT section 3, DESIGN_EVAL D5): each (mutant, level) >= 29/30 and aggregate one-sided
Clopper-Pearson 95 % lower bound >= 0.95. Stdlib + numpy; 0 tokens. Rows: vqa.eval-row/v1 JSONL."""
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

from eval import splits as SPL
from tests.px_mutants import driver as D
from visioncore import canon
from vadapt.synth import apply_op, render_scenario
from vadapt.synth.synthetic import _animate

N_FRAMES = 40
N_BASES = 6
BAND3 = (0, 112, 256, 48)  # animated band, 30 % of the tiles: keeps frame coverage >= COV_FROZEN_PPM
MAX_Y = 104  # rects stay above the band
# ADV_SEAL1 1: G-E3 runs only on the pre-registered held-out keys of eval/splits/x4_v3.json (seeds 2000..2029);
# x4_v2.json (seeds 1000..1029) was CONSUMED by W6 at a66d99e and is retired (audit pin only)
MANIFEST_SHA256 = "5269d68e99d1d421b1ca7b43055ad0c9b0bde812218aabb0176890ba29584b5d"
MANIFEST_V2_SHA256 = "175bf43bdc5b3a60980c4d52325269b8aa904038cc5202a67a5917c4e55b7c28"
CLS_FLICKER, CLS_FROZEN = ("FLICKER",), ("FROZEN",)
# mutant -> (op, expected classes, level sizes (w,h) or per-level param, level names)
MUTANTS = {
    "flicker": {"op": "toggle_rect", "expect": CLS_FLICKER, "animate": False,
                "levels": [{"size": (64, 48)}, {"size": (32, 32)}, {"size": (16, 16)}]},
    "frozen_subregion": {"op": "freeze_rect", "expect": CLS_FROZEN, "animate": True,
                         "levels": [{"size": (64, 48)}, {"size": (32, 32)}, {"size": (16, 16)}]},
    "paste": {"op": "paste_rect", "expect": ("CHANGED", "MOVED", "FLICKER"), "animate": False,
              "levels": [{"size": (64, 48)}, {"size": (32, 32)}, {"size": (16, 16)}]},
    "shift": {"op": "shift_rect", "expect": ("CHANGED", "MOVED", "FLICKER"), "animate": False,
              "levels": [{"size": (48, 32), "px": 8}, {"size": (48, 32), "px": 3}, {"size": (48, 32), "px": 1}]},
    "scroll": {"op": "scroll_rect", "expect": ("CHANGED", "MOVED", "FLICKER"), "animate": False,
               "levels": [{"size": (48, 32), "px": 4}, {"size": (48, 32), "px": 2}, {"size": (48, 32), "px": 1}]},
}


def _pmf(n, i, p):
    if p <= 0.0 or p >= 1.0:
        return 1.0 if (p <= 0.0) == (i == 0) or (p >= 1.0 and i == n) else 0.0
    lg = math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
    return math.exp(lg + i * math.log(p) + (n - i) * math.log1p(-p))


def cp_lower(k, n, alpha=0.05):
    """One-sided Clopper-Pearson lower bound: p with P(X >= k | p) = alpha."""
    if k <= 0:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        p = (lo + hi) / 2
        tail = sum(_pmf(n, i, p) for i in range(k, n + 1))
        lo, hi = (lo, p) if tail > alpha else (p, hi)
    return lo


def cp_upper(k, n, alpha=0.05):
    """One-sided Clopper-Pearson upper bound: p with P(X <= k | p) = alpha."""
    if k >= n:
        return 1.0
    if k == 0:
        return 1.0 - alpha ** (1.0 / n)
    lo, hi = 0.0, 1.0
    for _ in range(80):
        p = (lo + hi) / 2
        head = sum(_pmf(n, i, p) for i in range(k + 1))
        lo, hi = (p, hi) if head > alpha else (lo, p)
    return hi


def draw(tag, seed, n):
    """Deterministic ints from sha256(tag|seed): no RNG library."""
    d = hashlib.sha256(f"{tag}|{seed}".encode()).digest()
    return [int.from_bytes(d[4 * i:4 * i + 4], "big") % n for i in range(8)]


def trial_spec(mutant, level, seed, W=256, H=160):
    m = MUTANTS[mutant]
    lv = m["levels"][level]
    w, h = lv["size"]
    r = draw(f"X3|{mutant}|{level}", seed, 1 << 30)
    x = 8 + r[0] % (W - w - 15)
    y = 8 + r[1] % (MAX_Y - h - 7)
    t0 = 8 + r[2] % 7  # 8..14
    n = N_FRAMES
    rect = (x, y, w, h)
    if m["op"] == "toggle_rect":
        params = {"frames": list(range(t0, n, 2))}
    elif m["op"] == "freeze_rect":
        params = {"from_frame": t0}
    elif m["op"] == "paste_rect":
        sx = (x + w + 16 + r[3] % 8) % (W - w)
        params = {"frames": list(range(t0, n)), "src": [sx, y]}
    elif m["op"] == "shift_rect":
        params = {"frames": list(range(t0, n)), "dx": lv["px"], "dy": 0}
    else:
        params = {"frames": list(range(t0, n)), "dx": lv["px"], "dy": 0}
    return {"mutant": mutant, "level": level, "seed": seed, "rect": rect, "op": m["op"], "params": params,
            "base_seed": 1 + seed % N_BASES, "animate_rect": m["animate"], "expect": list(m["expect"])}


def build_trial(spec):
    frames, _, _ = render_scenario("clean_static", spec["base_seed"], n=N_FRAMES)
    frames = [f.copy() for f in frames]
    for i, f in enumerate(frames):
        _animate(f, BAND3, spec["base_seed"], i)
        if spec["animate_rect"]:
            _animate(f, spec["rect"], spec["base_seed"] + 50, i)
    out, mut = apply_op(frames, spec["op"], spec["rect"], spec["params"])
    return out, mut["truth"]


def outside_dirty(g, cls, fields):
    """Dirty tile-frames outside the truth set and outside the (animated) band."""
    band = D.rect_tiles(g, BAND3)
    idx = {int(m): k for k, m in enumerate(g.mortons)}
    skip = np.zeros(g.N, bool)
    skip[[idx[m] for m in band]] = True
    truth = {(int(a), idx[int(b, 16)]) for a, b in (D.FIELD.match(f).groups() for f in fields)}
    return sum(1 for fr in range(1, cls.shape[0]) for k in np.nonzero((cls[fr] >= 2) & ~skip)[0] if (fr, int(k)) not in truth)


def load_manifest(path=SPL.X4_V3, pinned=MANIFEST_SHA256):
    return SPL.load(path, pinned)


def run_trial(spec, manifest=None):
    """With a manifest: the (mutant, level, seed) must be registered and the capture sha must match it BEFORE any
    classification (else rc 5 split_leak); the row records the manifest sha and its split."""
    key = (spec["mutant"], spec["level"], spec["seed"])
    if manifest is not None:
        if manifest.config["x3"]["frames"] != N_FRAMES:
            SPL.leak("config_not_registered", "X3", frames=N_FRAMES)
        manifest.entry("x3", key)
    frames, truth = build_trial(spec)
    cap = SPL.capture_sha(frames)
    entry = manifest.verify("x3", key, cap) if manifest is not None else None
    g, cls, _, _ = D.classify_frames(frames)
    want = {D.CLS.index(c) for c in spec["expect"]}
    idx = {int(m): k for k, m in enumerate(g.mortons)}
    tf = sorted({(int(a), idx[int(b, 16)]) for a, b in (D.FIELD.match(f).groups() for f in truth["fields"])})
    hits = [fr for fr, k in tf if fr >= 1 and int(cls[fr, k]) in want]
    row = {"schema": "vqa.eval-row/v1", "x": "X3", "item": f"{spec['mutant']}/L{spec['level']}", "tier": "PX",
           "mutant": spec["mutant"], "level": spec["level"], "seed": spec["seed"], "rep": 0,
           "rect": list(spec["rect"]), "truth_n": len(tf), "hits": len(hits),
           "first_hit": min(hits) if hits else None, "detected": bool(hits),
           "outside_dirty": outside_dirty(g, cls, truth["fields"]), "capture_sha256": cap}
    if entry is not None:
        row.update(split=entry["split"], manifest_sha256=manifest.sha)
    return row


def summarize(rows, seeds):
    cells, K, N = [], 0, 0
    for mu in MUTANTS:
        for lv in range(3):
            r = [x for x in rows if x["mutant"] == mu and x["level"] == lv]
            k = sum(x["detected"] for x in r)
            n = len(r)
            K, N = K + k, N + n
            cells.append({"mutant": mu, "level": lv, "k": k, "n": n, "cp95_lower_ppm": int(cp_lower(k, n) * 1e6) if n else 0,
                          "pass": n > 0 and k >= n - 1 and (n < 30 or k >= 29)})
    agg = int(cp_lower(K, N) * 1e6) if N else 0
    ok = bool(cells) and all(c["pass"] for c in cells) and agg >= 950000
    return {"schema": "vqa.eval-summary/v1", "x": "X3", "gate": "G-E3", "seeds": seeds, "trials": N, "detected": K,
            "aggregate_cp95_lower_ppm": agg, "cells": cells, "verdict": "PASS" if ok else "FAIL"}


def summarize_manifest(rows, manifest):
    """G-E3 over the pre-registered held-out keys only: every row bound to this manifest (sha, registered test key,
    capture sha), no duplicates (else rc 5 split_leak); PASS also needs every registered test key."""
    seen = set()
    for r in rows:
        if r.get("manifest_sha256") != manifest.sha:
            SPL.leak("manifest_sha_mismatch", "X3", seed=r.get("seed"))
        key = (r.get("mutant"), r.get("level"), r.get("seed"))
        e = manifest.verify("x3", key, r.get("capture_sha256"))
        if e["split"] != "test" or r.get("split") != "test":
            SPL.leak("not_test_seed", "X3", seed=r.get("seed"), label=r.get("split"))
        if key in seen:
            SPL.leak("duplicate_row", "X3", seed=r.get("seed"))
        seen.add(key)
    test = manifest.keys("x3", "test")
    seeds = len({k[2] for k in test})
    s = summarize(rows, seeds)
    complete = len(rows) == len(test)
    return dict(s, sample_complete=complete, verdict="PASS" if s["verdict"] == "PASS" and complete else "FAIL",
                manifest_sha256=manifest.sha, split_id=manifest.doc.get("split_id"))


def run_rows(seeds=30, out_dir=None, mutants=None, manifest=None):
    """Without a manifest: legacy seeds 0..seeds-1 (unit fixtures only). With a manifest: its registered test keys
    (first `seeds` seeds), refusing anything unregistered; the full set is summarised against the manifest."""
    rows = []
    if manifest is not None:
        keys = [k for k in manifest.keys("x3", "test") if mutants is None or k[0] in mutants]
        per = sorted({k[2] for k in keys})[:seeds]
        rows = [run_trial(trial_spec(mu, lv, s), manifest) for mu, lv, s in keys if s in per]
        summ = summarize_manifest(rows, manifest) if mutants is None else None
    else:
        for mu in (mutants or MUTANTS):
            for lv in range(3):
                for s in range(seeds):
                    rows.append(run_trial(trial_spec(mu, lv, s)))
        summ = summarize(rows, seeds) if mutants is None else None
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "X3.jsonl").write_text("".join(canon.dumps(r) for r in rows))
        if summ:
            (out / "X3.summary.json").write_text(canon.dumps(summ))
    return rows, summ


def run(out_dir=None, seeds=30, manifest=None):
    """Runner entry point (E3 run_all): held-out keys of the pinned manifest only -> summary dict (PASS|FAIL)."""
    return run_rows(seeds, out_dir, manifest=manifest or load_manifest())[1]


def main(argv=None):
    ap = argparse.ArgumentParser(description="X3 PX power (G-E3)")
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"))
    a = ap.parse_args(argv)
    summ = run(a.out, a.seeds)
    sys.stdout.write(canon.dumps({"code": "OK" if summ["verdict"] == "PASS" else "E_GATE", "rc": 0 if summ["verdict"] == "PASS" else 5,
                                  "gate": "G-E3", "verdict": summ["verdict"], "detected": summ["detected"], "trials": summ["trials"],
                                  "aggregate_cp95_lower_ppm": summ["aggregate_cp95_lower_ppm"]}))
    return 0 if summ["verdict"] == "PASS" else 5


if __name__ == "__main__":
    sys.exit(main())
