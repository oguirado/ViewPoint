"""X2 cross-run CHANGED rate by render_index (O9 switch). A = clean capture, B = same scene re-rendered with extra
+-1 LSB GPU nondeterminism. Tiles compared at equal render_index with the S2 residual r(a,b) > T_enter.
Rate > 5 % (50000 ppm) => diff-region answers at keyframe granularity only. Control: B shifted by one render_index.
Usage: python3 -m eval.x2_crossrun --out <dir>"""
import argparse
import sys
from pathlib import Path

import numpy as np

from visioncore import canon, change, pipeline
from vadapt.synth import synthetic as syn

PAIRS = (("clean_static", 1), ("clean_static", 2), ("gpu_jitter", 1), ("gpu_jitter", 2), ("scroll", 1), ("scroll", 2))
LIMIT_PPM = 50000
EXT_TAG = 900


def _jitter(frames, seed):
    out = []
    for f, img in enumerate(frames):
        m = syn.noise(seed, EXT_TAG + f, img.shape)
        d = np.where(m & 7 == 0, np.where(m & 8, 1, -1), 0).astype(np.int16)
        out.append(np.clip(img.astype(np.int16) + d, 0, 255).astype(np.uint8))
    return out


def _tiles(frames, name, seed):
    run = pipeline.analyze(syn.SynthSource(frames, syn.build_manifest(frames, name, seed)))
    return run.tiles, run.grid


def changed_ppm(ta, tb, offset=0):
    """ppm of tile-frames with r(a_t, b_{t+offset}) > T_enter, over the overlapping render_index range."""
    t_enter = change.thresholds()[0]
    n = len(ta)
    lo, hi = max(0, -offset), min(n, n - offset)
    tot = bad = 0
    for t in range(lo, hi):
        r = change.residual(ta[t], tb[t + offset])
        bad += int((r > t_enter).sum())
        tot += len(r)
    return bad * 1000000 // max(tot, 1)


def run(out_dir=None, pairs=PAIRS, W=syn.DEF_W, H=syn.DEF_H, n=syn.DEF_N):
    rows, worst = [], 0
    for name, seed in pairs:
        fa, _, _ = syn.render_scenario(name, seed, W, H, n)
        fb = _jitter(fa, seed)
        ta, _ = _tiles(fa, name, seed)
        tb, _ = _tiles(fb, name, seed)
        ppm = changed_ppm(ta, tb, 0)
        ctl = changed_ppm(ta, tb, 1)
        worst = max(worst, ppm)
        rows.append({"schema": "vqa.eval-row/v1", "x": "X2", "item": "%s@s%d" % (name, seed), "tier": "PX", "rep": 0,
                     "rc": 0, "kill": None, "changed_ppm": ppm, "control_shift1_ppm": ctl,
                     "keyframe_only": ppm > LIMIT_PPM, "absence_reason": "synthetic pair; real run pairs AUSENTE"})
    keyframe = sum(r["keyframe_only"] for r in rows)
    summary = {"schema": "vqa.eval-summary/v1", "x": "X2", "gate": "O9", "pairs": len(rows), "worst_changed_ppm": worst,
               "limit_ppm": LIMIT_PPM, "keyframe_only_pairs": keyframe,
               "granularity": "keyframe" if keyframe else "tile", "verdict": "PASS" if keyframe == 0 and rows else "SWITCH",
               "source": "synthetic"}
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        canon.write_atomic(out / "X2.jsonl", "".join(canon.dumps(r) for r in rows).encode())
        canon.write_atomic(out / "X2.summary.json", canon.dumps(summary).encode())
    return rows, summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    _, s = run(a.out)
    sys.stdout.write(canon.dumps(dict(s, code="OK", rc=0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
