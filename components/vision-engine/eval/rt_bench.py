"""RT1-L0 baseline bench: per-stage Python timing of the sealed S0-S5 modules at 720p/1080p/4K on 1 or 5 X925 cores.
Stages (per frame, microseconds): S0 pad+luma+words_eq, S1 desc._compute, S1_rest (rest of describe_padded), S1b fill_xh64,
S2 classify, S3S4 regions+links, GOP (S5 amortised per frame), S0-S4 (sum of all but GOP). Frame 0 (keyframe) is skipped.
cores>1: S1 = parallel.describe_many(workers=N) on frames[1:] (frame 0 described outside the timer), wall amortised per
frame minus a separately measured FORK row (pool start/join on a tiny grid, total us per pass); no tile reuse. S1 runs in
parallel, everything else is serial in the consumer; the pad inside S1 is not counted again in S0 (S0 = luma + words_eq). Frames live only in a
RamWorkspace (tool rt_bench); nothing touches disk. cpus must lie in 5-9 (X925, E_RT_CPU).
Usage: taskset -c 5 python3 -m eval.rt_bench --res 720p --cores 1 --frames 60 --scenario gpu_jitter --out eval/results/rt1_bench.json"""
import argparse
import json
import os
import sys
import time

THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
NUMPY_PRELOADED = "numpy" in sys.modules   # then the env set below is too late for this process
for _v in THREAD_VARS:
    os.environ.setdefault(_v, "1")
REEXEC_MARK = "RT_BENCH_REEXEC"

import numpy as np  # noqa: E402  (after the thread env on purpose)

from vadapt import ramws  # noqa: E402
from vadapt.synth import synthetic as syn
from visioncore import change, desc, parallel, regions as rg
from visioncore import params as P
from visioncore.grid import Grid
from visioncore.pipeline import VisionRun, _run_gop

RES = {"720p": (1280, 720), "1080p": (1920, 1080), "4k": (3840, 2160)}
ALLOWED_CPUS = frozenset(range(5, 10))
STAGES = ("S0", "S1", "S1_rest", "S1b", "S2", "S3S4", "GOP", "S0-S4")
EXTRA_STAGES = ("FORK",)            # emitted only when cores > 1
PARALLEL_STAGES = ("S1",)           # stages that run in the worker pool when cores > 1
LOAD_LIMIT = 3.0                    # load1 at start >= this -> rows carry loaded_host=true
X5_REF = {"res": "720p", "cores": 1, "median_us": 8996, "p95_us": 9395, "tolerance": 0.25}
CAMPAIGN, TOOL = "rt1", "rt_bench"
METHOD = ("median of per-repeat medians / median of per-repeat p95; frame 0 skipped; OMP/OPENBLAS/MKL threads=1 set before "
          "numpy import; process pinned to cpus[:cores] inside the 5-9 affinity of the single invocation")


class BenchError(Exception):
    def __init__(self, code, msg=""):
        super().__init__("%s: %s" % (code, msg))
        self.code = code


def check_cpus(affinity, cores):
    """affinity (iterable of cpu ids) must be a non-empty subset of 5-9 with >= cores cpus, else E_RT_CPU. -> sorted list."""
    cpus = sorted(set(affinity))
    bad = [c for c in cpus if c not in ALLOWED_CPUS]
    if not cpus or bad:
        raise BenchError("E_RT_CPU", "cpus %s outside 5-9 (use taskset -c 5 / 5-9)" % (bad or cpus))
    if len(cpus) < cores:
        raise BenchError("E_RT_CPU", "%d cores asked, affinity has %d" % (cores, len(cpus)))
    return cpus


def thread_env_missing(env=None):
    """THREAD_VARS not equal to '1' in env (default os.environ) -> list of names."""
    env = os.environ if env is None else env
    return [k for k in THREAD_VARS if env.get(k) != "1"]


def needs_reexec(env=None, preloaded=None):
    """True when numpy was imported before the thread env was set or a thread var is not '1', and no re-exec happened yet."""
    env = os.environ if env is None else env
    if env.get(REEXEC_MARK):
        return False
    return bool(thread_env_missing(env)) or (NUMPY_PRELOADED if preloaded is None else preloaded)


def _reexec(argv):
    env = dict(os.environ)
    for k in THREAD_VARS:
        env[k] = "1"
    env[REEXEC_MARK] = "1"
    os.execve(sys.executable, [sys.executable, "-m", "eval.rt_bench"] + list(argv), env)


def _pct(v, q):
    s = sorted(v)
    return s[min(len(s) - 1, (len(s) - 1) * q // 100)]


def _timed_compute(box):
    orig = desc._compute

    def wrapped(*a, **k):
        t = time.perf_counter_ns()
        try:
            return orig(*a, **k)
        finally:
            box[0] += time.perf_counter_ns() - t
    return orig, wrapped


def fork_cost_us(cores, cpus, table):
    """Total us of one describe_many pool start/join on a 64x64 grid with `cores` frames (compute is negligible)."""
    g = Grid(64, 64)
    fr = np.zeros((cores, 64, 64, 3), dtype=np.uint8)
    t = time.perf_counter_ns()
    parallel.describe_many(fr, g, cores, cpus[:cores], table, lazy_xh=True)
    return (time.perf_counter_ns() - t) // 1000


def run_pass(frames, W, H, cores, cpus):
    """One pass over `frames` (indexable uint8[n,H,W,3]) -> {stage: [us per frame, frame 0 skipped]}.
    cores > 1 adds t_["FORK"] = [total us of one pool start/join] (not per frame)."""
    table = dict(P.PARAMS)
    grid = Grid(W, H)
    shapes = (grid.s, grid.gw, grid.gh)
    n = len(frames)
    pre, pre0, pre_us, fork_us = None, None, 0.0, 0
    if cores > 1:
        fork_us = fork_cost_us(cores, cpus, table)
        pre0 = parallel.describe_many(frames[:1], grid, 1, None, table, lazy_xh=True)   # frame 0: outside the timer
        t = time.perf_counter_ns()
        pre = parallel.describe_many(frames[1:], grid, cores, cpus[:cores], table, lazy_xh=True)
        wall = (time.perf_counter_ns() - t) / 1000.0
        pre_us = max(0.0, wall - fork_us) / (n - 1)
    box = [0]
    orig, wrapped = _timed_compute(box)
    t_ = {s: [] for s in STAGES}
    run = VisionRun(grid, {"width": W, "height": H}, table, None, list(range(n)))
    state = prev_padded = prev_xh = prev_regs = prev_raw = None
    desc._compute = wrapped
    try:
        for k in range(n):
            c0 = time.perf_counter_ns()
            padded = grid.pad(frames[k])
            cp = time.perf_counter_ns()
            y = desc.luma_of_padded(padded)
            if pre is not None:
                words_eq = np.zeros(grid.N, dtype=bool) if prev_padded is None else \
                    (padded == prev_padded).reshape(grid.gh, grid.s, grid.gw, grid.s * 3).all(axis=(1, 3)).reshape(-1)[grid.flat]
            c1 = time.perf_counter_ns()
            s0 = (c1 - cp) if pre is not None else (c1 - c0)   # pad already inside the parallel S1
            box[0] = 0
            if pre is None:
                tiles, words_eq = desc.describe_padded(padded, grid, prev_padded, prev_xh, table,
                                                       prev_tiles=prev_raw, y_plane=y, lazy_xh=True)
                c2 = time.perf_counter_ns()
                s1, s1r = box[0], (c2 - c1) - box[0]
            else:
                tiles = (pre0[0] if k == 0 else pre[k - 1]).copy()
                c2 = time.perf_counter_ns()
                s1, s1r = int(pre_us * 1000), 0
            if state is None:
                state = change.ClassState.init(tiles)
                out, state, summ = change.classify_first(state, tiles, y_cur=y)
            else:
                out, state, summ = change.classify_tiles(state, tiles, words_eq, y_cur=y, grid=shapes, params=table)
            prev_raw = tiles
            out = out.copy()
            c3 = time.perf_counter_ns()
            desc.fill_xh64(out, padded, grid, prev_xh, hash_all=prev_xh is None)
            c4 = time.perf_counter_ns()
            regs = rg.regions_of(out, W, H, k)
            gmv = tuple(summ["global_mv"] or (0, 0))
            link = rg.match_regions(prev_regs, regs, gmv, grid.s) if prev_regs is not None else \
                {"schema": "vqa.core-link/v1", "frame": k, "links": [], "split": [], "merge": []}
            c5 = time.perf_counter_ns()
            run.tiles.append(out)
            run.regions.append(regs)
            run.links.append(link)
            run.global_mv.append(gmv)
            prev_padded, prev_xh, prev_regs = padded, out["xh64"], regs
            if k:
                for s, v in (("S0", s0), ("S1", s1), ("S1_rest", s1r), ("S2", c3 - c2), ("S1b", c4 - c3),
                             ("S3S4", c5 - c4)):
                    t_[s].append(v // 1000)
                t_["S0-S4"].append((s0 + s1 + s1r + (c3 - c2) + (c4 - c3) + (c5 - c4)) // 1000)
    finally:
        desc._compute = orig
    g0 = time.perf_counter_ns()
    run.frame_nums = list(range(n))
    run.stages = {}
    _run_gop_timed(run)
    gus = (time.perf_counter_ns() - g0) // 1000 // n
    t_["GOP"] = [gus] * (n - 1)
    if cores > 1:
        t_["FORK"] = [fork_us]
    return t_


def _run_gop_timed(run):
    # frame_rows are not needed by encode_run; the sealed S5 driver is called as pipeline does
    _run_gop(run)


def summarize(passes):
    """passes: list of {stage:[us]} (one per repeat) -> {stage: (median_of_medians, median_of_p95)}."""
    out = {}
    for s in STAGES + tuple(x for x in EXTRA_STAGES if any(x in p for p in passes)):
        meds = [_pct(p[s], 50) for p in passes if p[s]]
        p95s = [_pct(p[s], 95) for p in passes if p[s]]
        out[s] = (int(_pct(meds, 50)), int(_pct(p95s, 50))) if meds else (0, 0)
    return out


def load_frames(ws, scenario, W, H, n, seed=1):
    """Render synthetic frames, park them in a ws file (np.memmap), drop the heap copy. -> memmap uint8[n,H,W,3]."""
    ws.reserve(n * W * H * 3)
    fr, _, _ = syn.render_scenario(scenario, seed, W, H, n)
    mm = np.memmap(ws.path("frames/frames.rgb24"), dtype=np.uint8, mode="w+", shape=(n, H, W, 3))
    for i, f in enumerate(fr):
        mm[i] = f
    del fr
    mm.flush()
    return mm


def bench(res, cores, frames, scenarios, repeats=1, ram_root=ramws.ROOT, affinity=None, meta=None):
    """-> rows. affinity: cpu ids the whole run may use (default: current); the process is pinned to cpus[:cores] while
    running and restored after. meta: dict merged into every row (method, argv, loaded_host...)."""
    prior = os.sched_getaffinity(0)
    cpus = check_cpus(prior if affinity is None else affinity, cores)
    use = cpus[:cores]
    if prior != set(use):
        os.sched_setaffinity(0, set(use))
    try:
        return _bench(res, cores, frames, scenarios, repeats, ram_root, use, meta or {})
    finally:
        os.sched_setaffinity(0, prior)


def _bench(res, cores, frames, scenarios, repeats, ram_root, use, meta):
    W, H = RES[res]
    rows = []
    for sc in scenarios:
        quota = frames * W * H * 3 + (64 << 20)
        with ramws.open_run(CAMPAIGN, "%s-%s-%d-%d" % (res, sc, cores, os.getpid()), quota, tool=TOOL, root=ram_root) as ws:
            mm = load_frames(ws, sc, W, H, frames)
            passes = [run_pass(mm, W, H, cores, use) for _ in range(repeats)]
            del mm
        load1 = round(os.getloadavg()[0], 2)
        for stage, (med, p95) in summarize(passes).items():
            row = {"res": res, "cores": cores, "scenario": sc, "stage": stage, "median_us": med, "p95_us": p95,
                   "load1": load1, "cpus": use, "frames": frames - 1, "repeats": repeats,
                   "parallel": cores > 1 and stage in PARALLEL_STAGES,
                   "unit": "us_per_pass" if stage in EXTRA_STAGES else "us_per_frame"}
            row.update(meta)
            rows.append(row)
    return rows


def x5_check(rows):
    """720p/1 core S0-S4 (gpu_jitter) vs X5 G-E4 ref within +-25 %; None when the row is absent."""
    for r in rows:
        if (r["res"], r["cores"], r["scenario"], r["stage"]) == ("720p", 1, "gpu_jitter", "S0-S4"):
            lo, hi = 1 - X5_REF["tolerance"], 1 + X5_REF["tolerance"]
            return {"median_us": r["median_us"], "p95_us": r["p95_us"], "ref": X5_REF,
                    "median_ratio": round(r["median_us"] / X5_REF["median_us"], 3),
                    "p95_ratio": round(r["p95_us"] / X5_REF["p95_us"], 3),
                    "within_25pct": lo * X5_REF["median_us"] <= r["median_us"] <= hi * X5_REF["median_us"]}
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", default="720p,1080p,4k")
    ap.add_argument("--cores", default="1")
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--scenario", default="gpu_jitter,clean_static,scroll")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--ram-root", default=ramws.ROOT)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    argv = list(sys.argv[1:] if argv is None else argv)
    start_load = round(os.getloadavg()[0], 2)
    initial = sorted(os.sched_getaffinity(0))
    env_ok = not thread_env_missing()
    meta = {"method": METHOD, "argv": argv, "loaded_host": start_load >= LOAD_LIMIT, "threads_env_ok": env_ok}
    rows = []
    try:
        for cores in [int(c) for c in a.cores.split(",")]:
            for res in a.res.split(","):
                if res not in RES:
                    raise BenchError("E_USAGE", res)
                rows += bench(res, cores, a.frames, a.scenario.split(","), a.repeats, a.ram_root, initial, meta)
    except BenchError as e:
        sys.stderr.write("%s\n" % e)
        return 2
    except ramws.RamError as e:
        sys.stderr.write("%s\n" % e)
        return 2
    doc = {"schema": "rt1.bench/v1", "rows": rows, "x5_check": x5_check(rows), "method": METHOD, "argv": argv,
           "start_load1": start_load, "loaded_host": start_load >= LOAD_LIMIT, "threads_env_ok": env_ok,
           "parallel_stages": list(PARALLEL_STAGES),
           "note": "frame 0 skipped everywhere (5c S1 times frames[1:]); GOP amortised per frame; cores>1: S1 = "
                   "describe_many wall minus FORK, amortised, no tile reuse; FORK = pool start/join us per pass; "
                   "pad counted once (inside S1) at cores>1; only S1 is parallel"}
    txt = json.dumps(doc, indent=1, sort_keys=True) + "\n"
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt)
    else:
        sys.stdout.write(txt)
    return 0


def _cli():
    argv = sys.argv[1:]
    if needs_reexec():
        _reexec(argv)
    return main(argv)


if __name__ == "__main__":
    sys.exit(_cli())
