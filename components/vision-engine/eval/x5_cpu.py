"""X5 CPU (G-E4): S0-S4 ms/frame at 1280x720 and 1920x1080, 1 core, decode excluded (frames pre-rendered in memory).
Per-frame cost = delta of time.process_time_ns between successive source pulls (frame k-1 processing); last frame and S5 excluded.
Values in microseconds (integers). Gate: pinned to exactly one core, median <= 15000 / 30000 us and p95 <= 25000 / 40000 us (720p / 1080p). Usage: python3 -m eval.x5_cpu --out <dir>"""
import argparse
import os
import sys
import time
from pathlib import Path

from visioncore import canon, pipeline
from vadapt.synth import synthetic as syn

SIZES = ((1280, 720, 15000), (1920, 1080, 30000))
P95_LIMITS = {"1280x720": 25000, "1920x1080": 40000}


def pin_one_core():
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    try:
        os.sched_setaffinity(0, {sorted(os.sched_getaffinity(0))[0]})
        return len(os.sched_getaffinity(0)) == 1  # verified, not assumed
    except (AttributeError, OSError):
        return False


class _TimedSource:
    def __init__(self, src):
        self.src, self.meta, self.marks = src, src.meta, []

    def __len__(self):
        return len(self.src)

    def __iter__(self):
        for f in self.src:
            self.marks.append(time.process_time_ns())
            yield f


def per_frame_us(W, H, n, scenario="gpu_jitter", seed=1):
    fr, _, _ = syn.render_scenario(scenario, seed, W, H, n)
    ts = _TimedSource(syn.SynthSource(fr, syn.build_manifest(fr, scenario, seed)))
    pipeline.analyze(ts)
    m = ts.marks
    return [(m[i + 1] - m[i]) // 1000 for i in range(len(m) - 1)]


def _pct(v, q):
    s = sorted(v)
    return s[min(len(s) - 1, (len(s) - 1) * q // 100)]


def gate_verdict(med_by_size, p95_by_size, pinned, sizes=SIZES, p95_limits=P95_LIMITS):
    """G-E4: one-core pin verified AND median AND p95 within budget for every size (missing size/value -> FAIL)."""
    if pinned is not True:
        return False
    for W, H, lim in sizes:
        k = "%dx%d" % (W, H)
        if k not in med_by_size or k not in p95_by_size or k not in p95_limits:
            return False
        if med_by_size[k] > lim or p95_by_size[k] > p95_limits[k]:
            return False
    return True


def run(out_dir=None, sizes=SIZES, n=200, repeats=3, p95_limits=P95_LIMITS):
    pinned = pin_one_core()
    rows = []
    for W, H, limit in sizes:
        for rep in range(repeats):
            v = per_frame_us(W, H, n)
            med, p95 = _pct(v, 50), _pct(v, 95)
            rows.append({"schema": "vqa.eval-row/v1", "x": "X5", "item": "%dx%d" % (W, H), "tier": "PX", "rep": rep,
                         "rc": 0, "kill": None, "cpu_ms_frame_med_milli": med, "cpu_ms_frame_p95_milli": p95,
                         "frames": len(v), "limit_med_milli": limit, "limit_p95_milli": p95_limits.get("%dx%d" % (W, H)),
                         "pinned_one_core": pinned, "pass": med <= limit and p95 <= p95_limits.get("%dx%d" % (W, H), -1),
                         "absence_reason": "synthetic gpu_jitter frames; decode excluded"})
    per_size, per_p95 = {}, {}
    for r in rows:
        per_size.setdefault(r["item"], []).append(r["cpu_ms_frame_med_milli"])
        per_p95.setdefault(r["item"], []).append(r["cpu_ms_frame_p95_milli"])
    med_of = {k: _pct(v, 50) for k, v in per_size.items()}
    p95_of = {k: _pct(v, 50) for k, v in per_p95.items()}
    verdict = gate_verdict(med_of, p95_of, pinned, sizes, p95_limits)
    summary = {"schema": "vqa.eval-summary/v1", "x": "X5", "gate": "G-E4", "pinned_one_core": pinned,
               "median_of_medians_us": med_of, "median_of_p95_us": p95_of,
               "p95_limits_us": {k: p95_limits.get(k) for k in med_of},
               "limits_us": {"%dx%d" % (W, H): lim for W, H, lim in sizes}, "frames": n, "repeats": repeats,
               "verdict": "PASS" if verdict else "FAIL", "source": "synthetic"}
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        canon.write_atomic(out / "X5.jsonl", "".join(canon.dumps(r) for r in rows).encode())
        canon.write_atomic(out / "X5.summary.json", canon.dumps(summary).encode())
    return rows, summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--frames", type=int, default=200)
    a = ap.parse_args(argv)
    _, s = run(a.out, n=a.frames)
    sys.stdout.write(canon.dumps(dict(s, code="OK" if s["verdict"] == "PASS" else "E_GATE", rc=0 if s["verdict"] == "PASS" else 5)))
    return 0 if s["verdict"] == "PASS" else 5


if __name__ == "__main__":
    sys.exit(main())
