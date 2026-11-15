"""End-to-end calibration runner (BDESIGN_TRUTH 2.7): bench scene -> Blender beauty + truth passes -> visioncore analyze
-> truth labels -> score -> calibrate -> calib_report.json + profile.json + SUMMARY.md.

  python3 -m blender_visual_qa.truth.run_bench --out <dir> [--benches B1,B2,...] [--frames 8] [--res 320x180] [--spp 4]

STORAGE LAW v1: every frame (PNG/EXR/truth .npy/.blend/vision tiles) lives in a vadapt.ramws RAM workspace
(/dev/shm/vqa-ramws/bvqa/<campaign>/<run_id>/); per chunk render -> decode -> release EXR, per unit (bench x seed x spp)
analyse -> rows + capture_sha256 -> copy L6 evidence to --out -> release. --ram-quota-mb (default 2048, max 8192),
--keep-frames MB (opt-in, <= 256 MB, build/kept_frames/). GATE units are rendered after T0 is frozen from FIT.

Up to three reserved-CPU Blender jobs (four with --cpu-plan w10), threads <= 4, tiny renders only
(<= 320x180, <= 60 frames captured in chunks of <= 8, spp tiers 4/16/64 via --spp 4,16,64). rc: 0 ok, 2 usage/environment/step error (E_*), 3 some bench failed (report still
written), 5 calibration refused (E_GATE truth_targets; report written, no profile).
"""
import argparse
import contextvars
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import threading
from pathlib import Path

import numpy as np

sys.dont_write_bytecode = True
from . import pin as _pin  # noqa: E402  JW9c-1: pin the configured wheel before any engine import

_ENGINE = _pin.pin()
from visioncore import canon, change, decode, frame as vframe, params as vparams, pipeline  # noqa: E402
from visioncore.errors import VqaError  # noqa: E402
from visioncore.grid import Grid  # noqa: E402
from vadapt.blender import from_render_dir  # noqa: E402
from vadapt import ramws  # noqa: E402  STORAGE LAW v1 RAM workspace (pinned wheel ve-sealed-1-law1)

from bpy_side.canon import cycles_seed

from . import calibrate, diag_moved, labels, score  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
BPY = REPO / "bpy_side"
BLENDER = os.environ.get("BVQA_BLENDER", "/usr/bin/blender")
BENCHES = ["B1", "B1x", "B2", "B3", "B4", "B4f", "B4n", "B5", "B5o", "B6", "B7", "B8", "BS"]
BENCH_NO = {b: i + 1 for i, b in enumerate(BENCHES)}
MAX_W, MAX_H, MAX_FRAMES, MAX_SPP, MAX_THREADS = 320, 180, 60, 64, 4
CHUNK_FRAMES = 8  # bounded capture/decode unit: beauty render, truth passes and EXR decode run per chunk
SPP_TIERS = (4, 16, 64)
CLEAN_SPP_START, CLEAN_SPP_CAP = 1024, 4096  # BTRUTH 4: clean capture spp, doubled while the 0-vs-1 self-check fails
# Only these benches seed per frame (bench_scenes seed_mode); every other bench uses ri = 0 for all its frames.
PER_FRAME_BENCHES = frozenset({"B4n", "B6"})
CLEAN_BENCHES = frozenset({"B3", "B5"})
CLEAN_MATCH_PERMILLE = 999  # self-check: >= 99.9 % of tiles within T_exit//4 (BTRUTH 4)
CLEAN_SEED_BIT = 19  # seed coordinates k >= 2^19 are reserved for clean captures
MAX_SEED_ARMS = 40
CPUS = "15-19"  # W10F: cpu 14 belongs to os-cu-0
# W9 host reservation excludes CPUs 5-9 and caps concurrent jobs at three.
CPU_PLANS = {"w9": ("15-19", "0-4", "10-13"), "w10": ("5-9", "15-19", "0-4", "10-14")}
JOB_CPUS = CPU_PLANS["w9"]
_JOB_CPU = contextvars.ContextVar("bench_cpu", default=CPUS)
_OPTIONS = contextvars.ContextVar("bench_options", default={})
RENDER_TIMEOUT = 300
_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()
_STOP = threading.Event()
DIRTY_TL = {"3", "4", "5", "6", "9", "10", "11"}
RAM_TOOL = "bvqa"
RAM_QUOTA_MB, RAM_QUOTA_MAX_MB = 2048, 8192
LOG_CAP = 256 << 10  # L6: persisted logs keep their last 256 KB
SKIP_EVIDENCE = frozenset({"xdg", "_exr", "clean_0", "clean_1", "determinism_repeat"})
TRUTH_FRAME_BYTES = 1094400  # z u32 + mv 2xi16 + uv 2xu16 + n 3xi8 + ob u16 + ma u16 at 320x180 (STORAGE LAW 0)
EXR_FRAME_BYTES = 4600000  # [H] NONE EXR ~3-4.4 MB/f at 320x180 (STORAGE LAW 3)


class _Store:
    """Process-wide RAM workspace of one run_bench invocation (None outside main: direct callers keep old paths)."""
    ws = None
    lock = threading.Lock()
    keep_left = 0
    keep = []
    units = []
    build = None
    run_id = None
    argv_sha = None


def _ws_call(name, *args):
    if _Store.ws is None:
        return None
    with _Store.lock:
        return getattr(_Store.ws, name)(*args)


def _release(path):
    """L4: release a workspace path (no-op when absent or outside a workspace run)."""
    if _Store.ws is not None and Path(path).exists():
        _ws_call("release", str(path))


def admit_unit(w, h, nframes):
    """Admission before a unit renders: resync usage, then reserve its planned peak (truth + PNG + one EXR chunk)."""
    if _Store.ws is None:
        return
    scale = max(1, w * h)
    need = (nframes * (TRUTH_FRAME_BYTES + (64 << 10)) + CHUNK_FRAMES * EXR_FRAME_BYTES) * scale // (MAX_W * MAX_H)
    with _Store.lock:  # admission only: the reservation is checked, then dropped so ram_peak_bytes stays measured
        ws = _Store.ws
        ws.account()
        peak = ws.peak
        ws.reserve(int(need))
        ws.used, ws.peak = ws.used - int(need), peak


def unit_peak(path):
    try:
        return ramws._tree_bytes(Path(path)) if Path(path).exists() else 0
    except OSError:
        return 0


def persist_unit(src, dst):
    """L6: copy the evidence of a unit dir (never a frame-like file, see ramws.FRAME_EXT/FRAME_NAMES) into the persistent
    --out tree; logs are trimmed to their last LOG_CAP bytes. -> bytes written."""
    src, dst = Path(src), Path(dst)
    if not src.is_dir() or src.resolve() == dst.resolve():
        return 0
    n = 0
    for dp, dn, fn in os.walk(src):
        dn[:] = sorted(d for d in dn if d not in SKIP_EVIDENCE)
        for f in sorted(fn):
            if f.lower().endswith(ramws.FRAME_EXT) or f in ramws.FRAME_NAMES or f.startswith(".") or f.endswith(".tmp"):
                continue
            p = Path(dp) / f
            if p.is_symlink() or not p.is_file():
                continue
            t = dst / p.relative_to(src)
            t.parent.mkdir(parents=True, exist_ok=True)
            data = p.read_bytes()
            if f.endswith(".log") and len(data) > LOG_CAP:
                data = data[-LOG_CAP:]
            t.write_bytes(data)
            n += len(data)
    return n


def keep_unit(src, tag):
    """L7 --keep-frames: copy unit frames into build/kept_frames/<run_id>.<tag>/ within the remaining run cap."""
    if _Store.ws is None or _Store.keep_left <= 0 or not Path(src).is_dir():
        return
    with _Store.lock:
        cap_mb = _Store.keep_left >> 20
        if cap_mb <= 0:
            _Store.keep_left = 0
            return
        keep = ramws.keep_frames(src, _Store.build, "%s.%s" % (_Store.run_id, tag), cap_mb, now=int(time.time()),
                                 reason="run_bench --keep-frames", argv_sha=_Store.argv_sha)
        _Store.keep_left -= keep["bytes"]
        if keep["kept_truncated"]:
            _Store.keep_left = 0
        _Store.keep.append(dict(keep, tag=tag))


def finish_unit(ws_root, bench, out_root, result=None, tag=None):
    """Unit end (L4, ordering of STORAGE LAW 7): rows/capture_sha256 are already in `result`; record the unit peak, copy
    evidence to out_root/bench, optionally keep frames, then release the unit (and its determinism repeat)."""
    if _Store.ws is None:
        return
    unit = Path(ws_root) / bench
    peak = unit_peak(unit) + unit_peak(Path(ws_root) / "determinism_repeat" / bench)
    persist_unit(unit, Path(out_root) / bench)
    if tag:
        keep_unit(unit, tag)
    if result is not None:
        result["ram_peak_bytes"] = peak
        result["run_dir"] = str(Path(out_root) / bench)
    with _Store.lock:
        _Store.units.append({"bench": bench, "tag": tag, "ram_peak_bytes": peak})
    _release(unit)
    _release(Path(ws_root) / "determinism_repeat" / bench)


class BenchError(Exception):
    def __init__(self, code, msg, rc=2):
        super().__init__("%s: %s" % (code, msg))
        self.code, self.msg, self.rc = code, msg, rc


def parse_seeds(text):
    """Parse logical R-B9 arms; overlapping FIT/GATE data is a gate failure."""
    result = {}
    for entry in text.split(","):
        match = re.fullmatch(r"(fit|gate):(\d+)(?:-(\d+))?", entry)
        if match is None or match[1] in result:
            raise BenchError("E_TRUTH_ARGS", "invalid --seeds")
        lo, hi = int(match[2]), int(match[3] or match[2])
        if not 0 <= lo <= hi < 1 << CLEAN_SEED_BIT or hi-lo >= MAX_SEED_ARMS:
            raise BenchError("E_TRUTH_ARGS", "seed arm range must contain at most 40 entries")
        result[match[1]] = list(range(lo, hi+1))
    if set(result) != {"fit", "gate"}:
        raise BenchError("E_TRUTH_ARGS", "FIT and GATE are required")
    if set(result["fit"]) & set(result["gate"]):
        raise BenchError("E_GATE", "fit_gate_overlap", rc=5)
    return result


def beauty_seed(bench, k, ri, mode):
    if mode not in ("fixed", "per_frame"):
        raise BenchError("E_TRUTH_ARGS", "seed_mode")
    return cycles_seed(BENCH_NO[bench], k, ri if mode == "per_frame" else 0)


def seed_expression(bench, k, mode, spp):
    beauty_seed(bench, k, 0, mode)  # validation before starting Blender
    expr = ("import bpy,sys;sys.path.insert(0,%r);from bpy_side.canon import cycles_seed;"
            "s=bpy.context.scene;s.cycles.device='CPU';s.cycles.use_animated_seed=False;"
            "s.cycles.seed=cycles_seed(%d,%d,0);s.cycles.samples=%d" % (str(REPO), BENCH_NO[bench], k, spp))
    if mode == "per_frame":
        expr += (";bpy.app.handlers.render_pre.append(lambda sc,*a:setattr(sc.cycles,'seed',"
                 "cycles_seed(%d,%d,sc.frame_current)))" % (BENCH_NO[bench], k))
    return expr


def scene_slot(bench, k):
    """Twin of bench_scenes.scene_slot (bpy boundary: no bpy_side import): image offset of the first jittered object."""
    i = (37 * k + 11 * BENCH_NO[bench]) % 169
    return i % 13 - 6, i // 13 - 6


def check_scene_collisions(benches, seeds):
    """W10S: seeded scenes of one plan must differ per seed (FIT/GATE captures disjoint by construction)."""
    for bench in benches:
        seen = {}
        for k in sorted(set(seeds)):
            slot = scene_slot(bench, k)
            if slot in seen:
                raise BenchError("E_TRUTH_ARGS", "scene collision %s seeds %d/%d" % (bench, seen[slot], k))
            seen[slot] = k


def check_seed_collisions(benches, seeds, nframes, per_frame=PER_FRAME_BENCHES):
    """Distinctness of every planned cycles seed: ri = 0..nframes for per-frame benches, ri = 0 for fixed ones."""
    seen = {}
    for bench in benches:
        for k in sorted(set(seeds)):
            for ri in (range(nframes + 1) if bench in per_frame else (0,)):
                seed = beauty_seed(bench, k, ri, "per_frame")
                coord = (bench, k, ri)
                if seed in seen and seen[seed] != coord:
                    raise BenchError("E_TRUTH_ARGS", "seed collision %r %r" % (seen[seed], coord))
                seen[seed] = coord


def blender_cmd(*args, threads=MAX_THREADS):
    cmd = [BLENDER, "-b", "--factory-startup", "-noaudio", "--python-exit-code", "9", "-t", str(threads)] + list(args)
    if not shutil.which("taskset"):
        raise BenchError("E_AFFINITY", "taskset AUSENTE")
    return ["taskset", "-c", os.environ.get("BVQA_CPU_CAP") or _JOB_CPU.get()] + cmd


def run_blender(cmd, scratch, log, timeout=RENDER_TIMEOUT):
    """Run one Blender in its own process group; killed on timeout. -> returncode (raises BenchError on timeout)."""
    xdg = Path(scratch) / "xdg"
    (xdg / "empty").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, XDG_CONFIG_HOME=str(xdg), XDG_DATA_HOME=str(xdg), XDG_CACHE_HOME=str(xdg), HOME=str(xdg),
               BLENDER_USER_SCRIPTS=str(xdg / "empty"), PYTHONNOUSERSITE="1", OMP_NUM_THREADS=str(MAX_THREADS),
               TZ="UTC", LC_ALL="C")
    with open(log, "ab") as fh:
        fh.write(("$ " + " ".join(cmd) + "\n").encode())
        with _ACTIVE_LOCK:
            if _STOP.is_set():
                raise BenchError("E_INTERRUPTED", "run stopped")
            p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            _ACTIVE.add(p.pid)
            if _STOP.is_set():
                os.killpg(p.pid, signal.SIGKILL)
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            raise BenchError("E_TIMEOUT", "blender exceeded %d s" % timeout)
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            with _ACTIVE_LOCK:
                _ACTIVE.discard(p.pid)


def _stop_children(_signum, _frame):
    _STOP.set()
    # Do not take a lock from a signal handler: a signal may interrupt its owner.
    for pid in tuple(_ACTIVE):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _need(rc, step, log):
    if rc != 0:
        raise BenchError("E_STEP", "%s rc=%d (see %s)" % (step, rc, log))


# --- stages ---------------------------------------------------------------------------------------------------------
def chunk_ranges(nframes, size=CHUNK_FRAMES):
    """Inclusive (first, last) render-index chunks covering 1..nframes, each at most `size` frames."""
    return [(lo, min(lo + size - 1, nframes)) for lo in range(1, nframes + 1, size)]


def render_bench(bench, run, w, h, nframes, spp, seed, scene_seed=None):
    """bench_scenes -> beauty PNGs (render/frame_NNNN.png) -> truth passes (truth/), captured and decoded in chunks
    of <= CHUNK_FRAMES frames (passes.py merges frames.jsonl/pending.jsonl by render_index). Returns the scene manifest.
    scene_seed k (W10S, seed arms) builds the seeded scene (bench_scenes --seed k); None keeps the golden scene."""
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True)
    log = run / "blender.log"
    blend = run / ("%s.blend" % bench)
    _need(run_blender(blender_cmd("--python", str(BPY / "bench_scenes.py"), "--", "--bench", bench, "--out", str(blend),
                                  "--res", "%dx%d" % (w, h), "--frames", str(nframes), "--samples", str(spp),
                                  *(() if scene_seed is None else ("--seed", str(scene_seed)))),
                      run, log), "bench_scenes", log)
    sj = run / ("%s.scene.json" % bench)
    scene = json.loads(sj.read_text())
    mode = scene.get("seed_mode", "fixed")
    if (mode == "per_frame") != (bench in PER_FRAME_BENCHES):
        raise BenchError("E_TRUTH_ARGS", "seed_mode %s disagrees with the pre-registered seed plan for %s" % (mode, bench))
    render = run / "render"
    render.mkdir(exist_ok=True)
    opts = _OPTIONS.get()
    expr = seed_expression(bench, seed, mode, spp)
    exr = run / "_exr"
    for lo, hi in chunk_ranges(nframes):
        _need(run_blender(blender_cmd(str(blend), "--python-expr", expr, "-o", str(render / "frame_####"), "-F", "PNG",
                                      "-s", str(lo), "-e", str(hi), "-a"), run, log), "beauty render", log)
        pass_args = [str(blend), "--python", str(BPY / "passes.py"), "--", "--run", str(run),
                     "--frames", "%d:%d" % (lo, hi), "--truth-seed", "0", "--truth-spp", "1", "--scene-json", str(sj)]
        if _Store.ws is not None:  # L2: EXR scratch inside the unit's RAM workspace dir
            pass_args += ["--exr-dir", str(exr)]
        if opts.get("decode", "inblender") == "external":
            pass_args += ["--exr-codec", "NONE", "--decode", "external"]
        if opts.get("determinism_check") and lo == 1:
            pass_args.append("--determinism-check")
        _need(run_blender(blender_cmd(*pass_args), run, log), "passes", log)
        if opts.get("decode") == "external":
            cap = os.environ.get("BVQA_CPU_CAP")
            cmd = ["taskset", "-c", cap or "0-4,10-13", sys.executable, "-B", "-m",
                   "blender_visual_qa.truth.decode_worker", "--pending", str(run / "truth" / "pending.jsonl"),
                   "--truth-dir", str(run / "truth")] + (["--cpus", cap] if cap else [])
            _need(run_blender(cmd, run, log), "external decode", log)
        _ws_call("account")  # peak includes this chunk's EXR + decoded truth; E_RAM_QUOTA stops the unit
        _release(exr)  # L4: EXR released right after its chunk is decoded
    (render / "scene.json").write_text(json.dumps(
        {"app": "blender", "scene": bench.lower(), "build_seq": 0, "fps_num": 24, "fps_den": 1,
         "adapter_version": "1+cycles-cpu-det"}, sort_keys=True))
    for name in ("journal.json", "scene.manifest.json"):  # labels.run reads both; bench manifest carries both blocks
        shutil.copyfile(sj, run / name)
    return scene


def render_clean(bench, run, w, h, nframes, spp, which):
    """Clean (high-spp) beauty capture of the same scene into <run>/clean_<which>/render with an independent seed
    (which = 0 or 1, BTRUTH 4); chunked like the noisy capture. Returns the clean run dir."""
    run = Path(run)
    cdir = run / ("clean_%d" % which)
    render = cdir / "render"
    render.mkdir(parents=True, exist_ok=True)
    blend, log = run / ("%s.blend" % bench), run / "blender.log"
    expr = ("import bpy;s=bpy.context.scene;s.cycles.device='CPU';s.cycles.use_animated_seed=False;"
            "s.cycles.seed=%d;s.cycles.samples=%d" % (cycles_seed(BENCH_NO[bench], 1 << CLEAN_SEED_BIT | which, 0), spp))
    for lo, hi in chunk_ranges(nframes):
        _need(run_blender(blender_cmd(str(blend), "--python-expr", expr, "-o", str(render / "frame_####"), "-F", "PNG",
                                      "-s", str(lo), "-e", str(hi), "-a"), run, log), "clean render", log)
        _ws_call("account")
    (render / "scene.json").write_text((run / "render" / "scene.json").read_text())
    return cdir


def _analysis_receipt(output):
    """Read the immutable W10R receipt; missing or malformed evidence never authorizes reuse."""
    try:
        receipt = json.loads((output / "analysis.receipt.json").read_text())
        if receipt["schema"] != "bvqa.analysis-receipt/v1":
            raise ValueError("receipt schema")
        if not isinstance(receipt["files"], dict):
            raise ValueError("invalid file receipt")
        vision = (output / "vision.json").read_bytes()
        if canon.sha256_bytes(vision) != receipt["files"]["vision.json"]:
            raise ValueError("vision.json hash")
        if json.loads(vision)["params_sha256"] != receipt["params_sha256"]:
            raise ValueError("params receipt mismatch")
        return receipt
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BenchError("E_VISION_REUSE", "%s: invalid analysis receipt: %s" % (output, exc)) from exc


def _reuse_analysis(output, man, table, receipt):
    """Verify input identity and every published byte before reconstructing the engine result."""
    try:
        if receipt["manifest_sha256"] != canon.sha256_bytes(man.read_bytes()):
            raise ValueError("manifest sha mismatch")
        if receipt["params_sha256"] != vparams.params_sha256(table):
            raise ValueError("params sha mismatch")
        cpu_ms = receipt["cpu_ms_per_frame"]
        if type(cpu_ms) is not int or cpu_ms < 0:
            raise ValueError("invalid timing receipt")
        files = receipt["files"]
        if not set(pipeline.FILES) <= files.keys():
            raise ValueError("incomplete output receipt")
        blobs = {}
        for name, digest in files.items():
            if Path(name).name != name or name in (".", "..", "analysis.receipt.json"):
                raise ValueError("invalid output filename")
            blobs[name] = (output / name).read_bytes()
            if canon.sha256_bytes(blobs[name]) != digest:
                raise ValueError("output hash mismatch: " + name)
        manifest = vframe.read_manifest(man)
        meta = {k: v for k, v in manifest.items() if k != "frames"}
        nums = [r.get("render_index", r["i"]) for r in manifest["frames"]]
        grid = Grid(meta["width"], meta["height"])
        frames_sha = vframe.frames_sha256(r["sha256"] for r in manifest["frames"])
        vrun = pipeline.VisionRun(grid, meta, dict(table), frames_sha, nums)
        data = blobs["tiles.bin"]
        hdr = pipeline.layout.parse_tile_header(data)
        if hdr["N"] != grid.N or hdr["n_frames"] != len(nums):
            raise ValueError("tile dimensions mismatch")
        tiles = np.frombuffer(data, dtype=pipeline.desc.TILE_DTYPE, offset=pipeline.layout.TILE_HDR_SIZE)
        vrun.tiles = [t.copy() for t in tiles.reshape(len(nums), grid.N)]
        vrun.frame_rows = [json.loads(line) for line in blobs["frames.jsonl"].splitlines()]
        regions = [json.loads(line) for line in blobs["regions.jsonl"].splitlines()]
        vrun.regions = [[r for r in regions if r["frame"] == n] for n in nums]
        vrun.links = [json.loads(line) for line in blobs["links.jsonl"].splitlines()]
        vrun.global_mv = [tuple(row["global_mv"] or (0, 0)) for row in vrun.frame_rows]
        vrun.stages = json.loads(blobs["vision.json"])["stages"]
        vrun.gop_files = {k: v for k, v in blobs.items() if k not in pipeline.FILES}
        pipeline._run_gop(vrun)
        # Round-trip through the pinned engine also verifies the header and vision metadata.
        if vrun._blobs() != blobs:
            raise ValueError("output round-trip mismatch")
        vrun.out_dir = output
        return vrun, cpu_ms
    except (OSError, ValueError, KeyError, TypeError, VqaError) as exc:
        raise BenchError("E_VISION_REUSE", "%s: cannot reuse analysis: %s" % (output, exc)) from exc


def vision_analyze(run, table, *, persist=True):
    """Adapter -> pipeline, or verified immutable output reuse. Returns (VisionRun, cpu_ms_per_frame).

    Clean capture retries use persist=False: only their residual is consumed, never their output files.
    """
    vin = Path(run) / "vision_in"
    from_render_dir.adapt(Path(run) / "render", vin)
    man = vin / "frames.manifest.json"
    t0 = time.process_time()
    output = Path(run) / "vision" if persist else None
    if output is not None and output.exists():
        if (output / "analysis.receipt.json").exists():
            receipt = _analysis_receipt(output)
            if receipt["params_sha256"] == vparams.params_sha256(table):
                return _reuse_analysis(output, man, table, receipt)
        else:
            try:
                previous = json.loads((output / "vision.json").read_text())
                if previous.get("params_sha256") == vparams.params_sha256(table):
                    raise BenchError("E_VISION_REUSE", "%s: analysis receipt absent" % output)
            except (OSError, ValueError, AttributeError) as exc:
                raise BenchError("E_VISION_REUSE", "%s: invalid legacy output: %s" % (output, exc)) from exc
        # Preserve legacy first-pass output, but never reuse it without a receipt.
        output = Path(run) / "vision_reanalysis" / vparams.params_sha256(table)
        if output.exists():
            return _reuse_analysis(output, man, table, _analysis_receipt(output))
    vrun = pipeline.analyze(pipeline.ManifestSource(man), table, None, output)
    n = max(1, len(vrun.tiles))
    cpu_ms = int((time.process_time() - t0) * 1000 / n)
    if output is not None and output.is_dir():
        receipt = {"schema": "bvqa.analysis-receipt/v1", "manifest_sha256": canon.sha256_bytes(man.read_bytes()),
                   "params_sha256": vrun.params_sha256, "cpu_ms_per_frame": cpu_ms,
                   "files": {p.name: canon.sha256_bytes(p.read_bytes()) for p in output.iterdir() if p.is_file()}}
        canon.write_atomic(output / "analysis.receipt.json", canon.dumps(receipt).encode())
    return vrun, cpu_ms


def raw_resid(vrun, table):
    """Raw q4 S1 residual r(t, t-1) per tile, Morton order, int64 [F, N]; row 0 zero. NOT the r>>2 display field."""
    hd = int(table["H_DEAD"])
    zero = np.zeros(vrun.grid.N, np.int64)
    return np.stack([zero] + [change.residual(vrun.tiles[f], vrun.tiles[f - 1], hd) for f in range(1, len(vrun.tiles))])


def truth_labels(run, resid=None, profile=None):
    """resid: clean-frame residual when a clean capture exists (JW9-3), else the noisy beauty residual as a declared
    stand-in. profile: the EFFECTIVE profile ({"T0_Q4": ...}); labels derive T_enter/T_exit from it, so fitted
    reanalysis changes the truth labels together with the engine thresholds. W10F: once the beauty is decoded
    (vision_in manifest, written by vision_analyze) tiles_truth.bin carries FLAG_UNOBSERVABLE (flag_unobservable,
    beauty + truth only) and res["unobservable_flagged"] is True; without decoded beauty the labels stay unflagged."""
    t0 = int((profile or {}).get("T0_Q4", vparams.effective(None)["T0_Q4"]))
    default = int(vparams.effective(None)["T0_Q4"])
    out = Path(run) / ("labels" if t0 == default else "labels_t0_%d" % t0)
    res = labels.run(str(run), dict(profile or {}), str(out), resid)
    tt = score.read_truth_tiles(out / "tiles_truth.bin")
    res["unobservable_flagged"] = (Path(run) / "vision_in" / "frames.manifest.json").exists()
    if res["unobservable_flagged"]:
        tt = flag_unobservable(run, tt, res, out, Grid(tt["hdr"]["W"], tt["hdr"]["H"]))
    return tt, res, out


def unobservable(rgbs, truths, labs, g):
    """W10F truth amendment: bool [F, N] (Morton) True on RIGID tiles of diag_moved cause a/b. The mask depends only
    on beauty and truth: diag_moved gets zero engine verdicts (its causes never read them)."""
    F, N = len(rgbs), g.N
    tiles = diag_moved.diagnose_sequence(rgbs, truths, np.zeros((F, N), np.int64), np.zeros((F, N, 2), np.int64), g,
                                         label_list=labs)
    out = np.zeros((F, N), bool)
    for f in range(1, F):
        out[f] = ~diag_moved.observable_mask([t for t in tiles if t["frame"] == f], N)
    return out


def flag_unobservable(run, tt, lres, ldir, g):
    """Set FLAG_UNOBSERVABLE in <ldir>/tiles_truth.bin from the decoded beauty (clean_0 capture when one exists) and
    the truth arrays; returns the re-read truth tiles. lres["sha256"] follows the repacked file."""
    run = Path(run)
    man = run / "clean_0" / "vision_in" / "frames.manifest.json"
    if not man.exists():
        man = run / "vision_in" / "frames.manifest.json"
    rgbs = [f.rgb for f in pipeline.ManifestSource(man)]
    truths = [labels.load_frame(str(run / "truth"), ri) for ri in lres["frame_ids"]]
    if not len(rgbs) == len(truths) == tt["tl"].shape[0]:
        raise BenchError("E_STALE", "beauty %d / truth %d / labels %d frames" % (len(rgbs), len(truths), tt["tl"].shape[0]), rc=4)
    labs = [{"tl": tt["tl"][f], "mv": tt["mv"][f]} for f in range(len(rgbs))]
    path = Path(ldir) / "tiles_truth.bin"
    blob = labels.mark_unobservable(path.read_bytes(), unobservable(rgbs, truths, labs, g))
    path.write_bytes(blob)
    lres["sha256"] = hashlib.sha256(blob).hexdigest()
    return score.read_truth_tiles(path)


def read_coverage(labels_dir, frame_ids, n_tiles):
    """frames_meta.jsonl -> cov (F, N) int64 aligned to the captured frames. Absent/misaligned metadata blocks the run
    (JW9-4): FROZEN strata would otherwise silently stay empty."""
    path = Path(labels_dir) / "frames_meta.jsonl"
    if not path.exists():
        raise BenchError("E_TRUTH_COVERAGE", "frames_meta.jsonl absent", rc=5)
    meta = {}
    for line in path.read_text().splitlines():
        d = json.loads(line)
        meta[d["frame"]] = d
    if sorted(meta) != list(frame_ids):
        raise BenchError("E_TRUTH_COVERAGE", "frames_meta frames %r != captured %r" % (sorted(meta), list(frame_ids)), rc=5)
    return np.stack([np.full(n_tiles, int(meta[f]["cov_ppm"]), np.int64) for f in frame_ids])


def capture_sha(run):
    """Identity of the captured data (truth arrays + decoded beauty) for FIT/GATE capture disjointness; None if absent."""
    try:
        return determinism_fingerprint(run)
    except (BenchError, OSError):
        return None


def validate_clean_pair(runs, grid, nframes):
    """Validate clean twin captures: population, dimensions, frame count and identities."""
    sources = [pipeline.ManifestSource(Path(r) / "vision_in" / "frames.manifest.json") for r in runs]
    if len(sources) != 2 or not 0 < nframes <= MAX_FRAMES or any(
            len(s) != nframes or (s.meta["width"], s.meta["height"]) != (grid.W, grid.H) for s in sources):
        raise BenchError("E_CLEAN_PAIR", "clean capture population or dimensions disagree")
    for a, b in zip(*sources):
        if (a.i, a.render_index, a.t_us) != (b.i, b.render_index, b.t_us):
            raise BenchError("E_CLEAN_PAIR", "clean capture frame identities disagree")
    return sources


def clean_pixel_diff(runs, grid, nframes, sources=None):
    """Twin-image distance: ceil(16 * mean(max_channel(abs(a-b)))) per real tile.

    B5's emissive checker can straddle an S1 chroma quantization boundary:
    arbitrarily small pixel noise then costs 8 q4, above the clean tolerance.
    Measure convergence before that discontinuous quantization. Do not pad
    partial tiles or round a nonzero discrepancy down to zero.
    """
    if sources is None:
        sources = validate_clean_pair(runs, grid, nframes)
    rows = []
    for a, b in zip(*sources):
        delta = np.abs(a.rgb.astype(np.int16) - b.rgb.astype(np.int16)).max(axis=2)
        row = []
        for x, y in zip(grid.tx, grid.ty):
            tile = delta[y * grid.s:min((y + 1) * grid.s, grid.H), x * grid.s:min((x + 1) * grid.s, grid.W)]
            row.append((16 * int(tile.sum()) + tile.size - 1) // tile.size)
        rows.append(row)
    return np.asarray(rows, dtype=np.int64)


def clean_resid(run, bench, table, w, h, nframes, spp0=CLEAN_SPP_START):
    """Independent clean residual (BTRUTH 4): two clean captures (seeds 0/1) at high spp; their per-tile S1 difference
    must be <= T_exit//4 on >= 99.9 % of tiles, else spp doubles up to CLEAN_SPP_CAP, then rc 6 E_BUDGET.
    W10B5: B5 validates convergence in image space (clean_pixel_diff), since
    S1 quantization is discontinuous; the temporal truth residual stays S1.
    Returns (resid [F, N] of clean t vs clean t-1, clean_spp)."""
    spp = spp0
    while True:
        runs = [render_clean(bench, run, w, h, nframes, spp, k) for k in (0, 1)]
        vruns = [vision_analyze(r, table, persist=False)[0] for r in runs]
        sources = validate_clean_pair(runs, vruns[0].grid, nframes)
        hd, t_exit = int(table["H_DEAD"]), labels.thresholds({"T0_Q4": int(table["T0_Q4"])})[1]
        diff = np.stack([change.residual(a, b, hd) for a, b in zip(vruns[0].tiles, vruns[1].tiles)])
        if bench == "B5":
            s1_diff = diff
            diff = clean_pixel_diff(runs, vruns[0].grid, nframes, sources=sources)
            receipt = {"schema": "bvqa.clean-agreement/v1", "metric": "rgb-max-mean-q4/v1", "spp": spp,
                       "threshold_q4": t_exit // 4, "match_permille": CLEAN_MATCH_PERMILLE,
                       "population": int(diff.size), "bad_tiles": int((diff > t_exit // 4).sum()),
                       "max_q4": int(diff.max()), "s1_bad_tiles": int((s1_diff > t_exit // 4).sum())}
            (Path(run) / "clean_agreement.json").write_text(canon.dumps(receipt))
        if int((diff <= t_exit // 4).sum()) * 1000 >= CLEAN_MATCH_PERMILLE * diff.size:
            return raw_resid(vruns[0], table), spp
        if spp * 2 > CLEAN_SPP_CAP:
            raise BenchError("E_BUDGET", "clean capture self-check failed up to %d spp" % CLEAN_SPP_CAP, rc=6)
        spp *= 2


def confusion(tl, cls_pred):
    """12 x 7 integer confusion (truth label x predicted class) over non-excluded tiles."""
    m = np.zeros((labels_tl_count(), 7), dtype=np.int64)
    keep = ~np.isin(tl, score.EXCLUDED)
    np.add.at(m, (tl[keep].astype(np.int64), cls_pred[keep].astype(np.int64)), 1)
    return m


def labels_tl_count():
    return score.TL_MAX + 1


def bench_metrics(row, conf):
    """Per-scene precision/recall (ppm): per expected-dirty tl recall, dirty precision, per class precision."""
    summ = score.summarize([row])
    dirty_pred = conf[:, score.DIRTY_MIN:].sum()
    tp = sum(int(conf[t, score.DIRTY_MIN:].sum()) for t, (k, _) in score.EXPECT.items() if k == "dirty")
    cls_prec = {}
    for c in range(7):
        col = int(conf[:, c].sum())
        good = sum(int(conf[t, c]) for t, (_, ok) in score.EXPECT.items() if c in ok)
        cls_prec[str(c)] = score.ppm(good, col)
    return {
        "tl": summ["tl"], "mv_exact_ppm": summ["mv_exact_ppm"], "mv_err_p95_px": summ["mv_err_p95_px"],
        "dirty_precision_ppm": score.ppm(tp, int(dirty_pred)),
        "jitter_fp_ppm": (summ["tl"].get("0") or {}).get("dirty_ppm"),
        "class_precision_ppm": cls_prec, "fp_resid_max_q4": summ["fp_resid_max_q4"],
    }


def analyze_bench(bench, run, spp, table, t_wall=None, seed=0, clean=None):
    """Analyze + label + score an already rendered run dir at `table` (no Blender). clean: optional clean residual
    [F, N] (from clean_resid) used for the truth predicates instead of the noisy beauty residual."""
    t_wall = time.time() if t_wall is None else t_wall
    try:
        vrun, cpu_ms = vision_analyze(run, table)
    except VqaError as exc:
        if exc.code == "E_VC_GRID_MISMATCH":
            raise BenchError("E_STALE", "reason grid", rc=4) from exc
        raise
    g = vrun.grid
    resid = raw_resid(vrun, table)
    cpath = Path(run) / "clean_resid.npy"
    if clean is None and cpath.exists():
        clean = np.load(cpath)
    # JW9b-3: a B3/B5 truth predicate never falls back to the noisy beauty residual; without clean evidence the
    # shading tiles stay SHADE_BAND (excluded) and the row is marked so calibrate refuses (clean_evidence_absent).
    label_resid = clean if clean is not None else (None if bench in CLEAN_BENCHES else resid)
    tt, lres, ldir = truth_labels(run, label_resid, {"T0_Q4": int(table["T0_Q4"])})
    if tt["tl"].shape[0] != len(vrun.tiles) or tt["tl"].shape[1] != g.N:
        raise BenchError("E_STALE", "truth %s vs vision %s" % (tt["tl"].shape, (len(vrun.tiles), g.N)), rc=4)
    cls = np.stack([t["cls"] for t in vrun.tiles]).astype(np.int64)
    mv = np.stack([np.stack([t["mv_dx"], t["mv_dy"]], axis=-1) for t in vrun.tiles]).astype(np.int64)
    for t in vrun.tiles:
        if not (np.diff(t["morton"].astype(np.int64)) > 0).all():
            raise BenchError("E_STALE", "vision tiles not in ascending Morton order", rc=4)
    cov = read_coverage(ldir, lres["frame_ids"], g.N)
    row = score.score_frames(tt["tl"], cls, tt["mv"], mv, bench, seed, spp, g.ident, int(table["T0_Q4"]), resid=resid, cov=cov,
                             flags=tt.get("flags"))
    row["capture_sha256"] = capture_sha(run)
    if bench in CLEAN_BENCHES:
        row["clean_resid"] = clean is not None
    evidence(row, bench, vrun, tt, cls, int(table["T0_Q4"]))
    conf = confusion(tt["tl"], cls)
    res = {"bench": bench, "seed": seed, "grid": g.ident, "frames": len(vrun.tiles), "tiles": g.N,
           "cpu_ms_per_frame": cpu_ms, "wall_s": int(time.time() - t_wall), "labels_sha256": lres["sha256"], "clean_resid": clean is not None,
           "unobservable_flagged": lres.get("unobservable_flagged"),
           "params_sha256": vrun.params_sha256, "metrics": bench_metrics(row, conf),
           "confusion": conf.tolist(), "row": row}
    if bench == "B6":  # still probe stand-in: per-tile S1 residual of the static per-frame-seed scene (frames >= 1)
        res["still_q4"] = [int(x) for x in resid[1:].reshape(-1).tolist()] if len(resid) > 1 else []
    return res


def evidence(row, bench, vrun, tt, cls, t0_q4):
    """JW9b-2: acceptance evidence produced from the actual stage outputs (never injected): C1b flips (B6), C5 select
    coverage via the engine selector (every bench), B5o global_mv (B5o), S4 best-region IoU (B1, B2)."""
    row["c5_select"] = score.c5_select(vrun.tiles, vrun.grid, tt["tl"], t0_q4)
    if bench == "B6":
        row["c1b_flips"] = score.c1b_flips(tt["tl"], cls)
    if bench == "B5o":
        row["b5o_global_mv"] = score.b5o_global_mv(tt["tl"], tt["mv"], vrun.global_mv)
    if bench in ("B1", "B2"):
        ious = score.s4_iou(tt["tl"], tt["ob"], vrun.tiles[0]["morton"], vrun.regions)
        if ious:
            row["s4_iou_ppm"], row["s4_iou_n"] = ious[(len(ious) - 1) // 2], len(ious)
    return row


def determinism_fingerprint(run):
    """Canonical truth-array bytes plus decoded beauty pixels; excludes timings and PNG metadata."""
    run = Path(run)
    files = sorted((run / "truth").glob("f*.npy"))
    beauty = sorted((run / "render").glob("frame_*.png"))
    if not files or not beauty:
        raise BenchError("E_DETERMINISM", "empty truth or beauty", rc=5)
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    for path in beauty:
        digest.update(path.name.encode())
        digest.update(decode.png(path).tobytes())
    return digest.hexdigest()


def run_one(bench, root, w, h, nframes, spp, table, seed=0, analyze=True, scene_seed=None):
    """Render (+ clean capture when requested) and, unless analyze=False (GATE arms wait for the frozen profile),
    analyze at `table`. analyze=False -> {"bench", "seed", "run_dir"} only."""
    run = Path(root) / bench
    t_wall = time.time()
    render_bench(bench, run, w, h, nframes, spp, seed, scene_seed)
    clean_spp = _OPTIONS.get().get("clean_spp", 0)
    if clean_spp and bench in CLEAN_BENCHES:
        resid, used = clean_resid(run, bench, table, w, h, nframes, clean_spp)
        np.save(run / "clean_resid.npy", resid)
        (run / "clean_spp.json").write_text(json.dumps({"clean_spp": used}) + "\n")
    if not analyze:
        return {"bench": bench, "seed": seed, "run_dir": str(run)}
    return analyze_bench(bench, run, spp, table, t_wall, seed=seed)


# --- reporting ------------------------------------------------------------------------------------------------------
def _p(v):
    return "-" if v is None else "%.1f%%" % (v / 10000.0)


def summary_md(report):
    L = ["# Blender truth bench - calibration summary", "",
         "grid `%s`, spp %d, %d frames, renderer `%s`, T0_Q4 fitted **%s** (still %s, fp %s), passed: **%s**" % (
             report["grid"], report["spp"], report["frames"], report["renderer"], report["calibration"].get("T0_Q4"),
             report["calibration"].get("T0_still"), report["calibration"].get("T0_fp"), report["calibration"].get("passed")),
         ]
    cal = report["calibration"]
    if cal.get("refusal") or cal.get("split_violations"):  # W10X: a refusal must not read as metric values None
        L += ["", "REFUSED: **%s**; split_violations %s; GATE scored at frozen T0_Q4 %s" % (
            cal.get("refusal"), cal.get("split_violations"), report.get("frozen_t0_q4"))]
        L += ["- overlap %s fit %s gate %s (capture %s)" % (d["bench"], d["fit"], d["gate"], d["capture_sha256"][:12])
              for d in cal.get("split_detail") or []]
    L += ["", "| scene | arm | seed | dirty precision | dirty recall by tl (n) | STATIC FP | mv exact | mv err p95 px | CPU ms/frame |",
          "|---|---|---|---|---|---|---|---|---|"]
    for b in report["benches"]:
        m = b["metrics"]
        rec = ", ".join("tl%s %s (%d)" % (t, _p(d["dirty_ppm"]), d["n"]) for t, d in sorted(m["tl"].items(), key=lambda kv: int(kv[0]))
                        if t in DIRTY_TL) or "-"
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %d |" % (b["bench"], b.get("seed_group", "-"), b.get("seed", "-"),
                                                                    _p(m["dirty_precision_ppm"]), rec,
                                                         _p(m["jitter_fp_ppm"]), _p(m["mv_exact_ppm"]),
                                                         m["mv_err_p95_px"], b["cpu_ms_per_frame"]))
    L += ["", "Class precision (ppm of predicted tiles whose truth label accepts that class), per scene:", ""]
    for b in report["benches"]:
        L.append("- %s: %s" % (b["bench"], ", ".join("c%s=%s" % (k, _p(v)) for k, v in
                                                   sorted(b["metrics"]["class_precision_ppm"].items()))))
    if report["failures"]:
        L += ["", "Failures:", ""] + ["- %s: %s" % (f["bench"], f["error"]) for f in report["failures"]]
    L += ["", "Checks:", ""] + ["- %s %s %s %s: %s" % (c["name"], c["op"], c["bound"], c["value"], c["status"])
                                for c in report["calibration"].get("checks", [])]
    return "\n".join(L) + "\n"


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="blender_visual_qa.truth.run_bench")
    ap.add_argument("--out", required=True)
    ap.add_argument("--benches", default=",".join(BENCHES))
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--res", default="%dx%d" % (MAX_W, MAX_H))
    ap.add_argument("--spp", default="4", help="spp tier or comma list from {4,16,64}; each tier runs as its own calibration")
    ap.add_argument("--renderer", default=None)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--seeds", default=None)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--cpu-plan", choices=sorted(CPU_PLANS), default=None,
                    help="declared worker CPU pinning: w9 (<= 3 jobs, excludes 5-9) or w10 (<= 4 jobs); "
                         "default w9 for --jobs <= 3, w10 for --jobs 4")
    ap.add_argument("--decode", choices=("inblender", "external"), default="inblender")
    ap.add_argument("--determinism-check", action="store_true")
    ap.add_argument("--clean-spp", type=int, default=None,
                    help="clean-capture spp for B3/B5 truth residuals (default: %d when --seeds calibrates B3/B5, else 0; "
                         "0 = off: B3/B5 shading stays SHADE_BAND and calibration refuses clean_evidence_absent)" % CLEAN_SPP_START)
    ap.add_argument("--ram-quota-mb", type=int, default=RAM_QUOTA_MB,
                    help="STORAGE LAW L3 per-run RAM workspace quota (default %d, max %d)" % (RAM_QUOTA_MB, RAM_QUOTA_MAX_MB))
    ap.add_argument("--keep-frames", type=int, default=0, metavar="MB",
                    help="L7 opt-in: copy up to MB (<= 256) of frames into build/kept_frames/ (TTL 24 h)")
    ap.add_argument("--run-id", default=None, help="RAM workspace run id (default: generated; never part of any hash)")
    ap.add_argument("--campaign", default="adhoc", help="RAM workspace campaign segment")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate the flags and the pinned engine, print the plan, render nothing (rc 0)")
    return ap.parse_args(argv)


def parse_spp(text):
    try:
        tiers = [int(v) for v in str(text).split(",")]
    except ValueError:
        raise BenchError("E_USAGE", "bad --spp %r" % text)
    if not tiers or len(tiers) != len(set(tiers)) or any(t not in SPP_TIERS for t in tiers):
        raise BenchError("E_BUDGET", "spp tiers must be distinct values of %r" % (SPP_TIERS,))
    return tiers


def validate(a):
    benches = [b for b in a.benches.split(",") if b]
    bad = [b for b in benches if b not in BENCHES]
    if not benches or bad or len(benches) != len(set(benches)):
        raise BenchError("E_USAGE", "unknown bench %r" % (bad or a.benches,))
    try:
        w, h = (int(v) for v in a.res.lower().split("x"))
    except ValueError:
        raise BenchError("E_USAGE", "bad --res %r" % a.res)
    if not (1 <= w <= MAX_W and 1 <= h <= MAX_H):
        raise BenchError("E_BUDGET", "res %dx%d exceeds %dx%d" % (w, h, MAX_W, MAX_H))
    if not 2 <= a.frames <= MAX_FRAMES:
        raise BenchError("E_BUDGET", "frames must be 2..%d" % MAX_FRAMES)
    a.spp_tiers = parse_spp(a.spp)
    a.cpu_plan = a.cpu_plan or ("w9" if a.jobs <= len(CPU_PLANS["w9"]) else "w10")
    if not 1 <= a.jobs <= len(CPU_PLANS[a.cpu_plan]):
        raise BenchError("E_BUDGET", "cpu plan %s: jobs must be 1..%d" % (a.cpu_plan, len(CPU_PLANS[a.cpu_plan])))
    a.seed_sets = parse_seeds(a.seeds) if a.seeds is not None else None
    if getattr(a, "clean_spp", None) is None:  # JW9b-3: calibration runs capture independent clean B3/B5 residuals
        a.clean_spp = CLEAN_SPP_START if a.seed_sets is not None and CLEAN_BENCHES & set(benches) else 0
    if not 0 <= a.clean_spp <= CLEAN_SPP_CAP:
        raise BenchError("E_BUDGET", "clean spp must be 0..%d" % CLEAN_SPP_CAP)
    if not 1 <= a.ram_quota_mb <= RAM_QUOTA_MAX_MB:
        raise BenchError("E_RAM_QUOTA", "--ram-quota-mb must be 1..%d" % RAM_QUOTA_MAX_MB, rc=5)
    if not 0 <= a.keep_frames <= ramws.KEEP_CAP_MB:
        raise BenchError("E_BUDGET", "--keep-frames must be 0..%d MB" % ramws.KEEP_CAP_MB)
    arms = [k for v in (a.seed_sets or {"legacy": [0]}).values() for k in v]
    check_seed_collisions(benches, arms, a.frames)
    if a.seed_sets is not None:
        check_scene_collisions(benches, arms)
    if not os.path.exists(BLENDER):
        raise BenchError("E_NO_BLENDER", BLENDER)
    return benches, w, h


def fit_t0(fit_results, still):
    """T0 fitted on FIT rows only via calibrate.fit_t0 (BTRUTH C1/C2), clamped to the engine bound."""
    return min(int(calibrate.fit_t0([r["row"] for r in fit_results], still)["T0_Q4"]), vparams.BOUNDS["T0_Q4"][1])


def _write_refusal(out, report, reason, rows=()):
    (out / "rows.jsonl").write_text("".join(canon.dumps(r["row"]) for r in rows))
    report.update(rc=5, calibration={"refusal": reason})
    (out / "calib_report.json").write_text(json.dumps(report, sort_keys=True, indent=1) + "\n")
    (out / "SUMMARY.md").write_text(summary_md(report))
    print(json.dumps({"code": "E_GATE", "rc": 5, "reason": reason}, sort_keys=True))
    return 5


def _incomplete(out, report, results, failures):
    done = [r for r in results if "row" in r]
    failed_rc = 5 if any(f.get("rc") == 5 for f in failures) else 3
    (out / "rows.jsonl").write_text("".join(canon.dumps(r["row"]) for r in done))
    report.update(rc=failed_rc, calibration={"refusal": "incomplete_population"}, failures=list(failures),
                  benches=_slim(results))
    (out / "calib_report.json").write_text(json.dumps(report, sort_keys=True, indent=1) + "\n")
    (out / "SUMMARY.md").write_text(summary_md(report))
    print(json.dumps({"code": "E_RUN", "rc": failed_rc, "failures": len(failures)}, sort_keys=True))
    return failed_rc


def _slim(results):
    return [{k: v for k, v in r.items() if k != "row"} for r in results]


def run_tier(a, benches, w, h, spp, out):
    """One spp tier: render all arms, fit T0 on FIT only, freeze it, then label/score FIT and GATE once at the
    frozen profile and calibrate. -> rc."""
    out.mkdir(parents=True, exist_ok=True)
    # A refused repeat must not leave a profile from an earlier accepted run.
    (out / "profile.json").unlink(missing_ok=True)
    table = vparams.effective(None)
    results, failures = [], []
    arms = a.seed_sets or {"legacy": [0]}
    tasks = [(b, group, k) for group in ("legacy", "fit", "gate") if group in arms for k in arms[group] for b in benches]
    plan = CPU_PLANS[a.cpu_plan]
    tier_rel = "spp_%d" % spp

    def roots(group, k):
        """(RAM workspace unit root, persistent evidence root) of one arm; same layout under both."""
        rel = "legacy" if group == "legacy" else "%s/seed_%06d" % (group, k)
        root = out if group == "legacy" else out / group / ("seed_%06d" % k)
        if _Store.ws is None:
            return root, root
        return _Store.ws.path("%s/%s/" % (tier_rel, rel)), root

    def failure(b, group, k, e):
        if isinstance(e, ramws.RamError):
            _STOP.set()  # L3: a quota breach stops the run cleanly; the units are released by their owners
        return {"bench": b, "seed": k, "seed_group": group, "rc": 5 if isinstance(e, ramws.RamError) else getattr(e, "rc", 2),
                "error": json.dumps(e.to_obj()) if isinstance(e, VqaError) else str(e)}

    errors = (BenchError, VqaError, OSError, ValueError, KeyError, ramws.RamError)

    def flight(slot, todo):
        completed = []
        token = _JOB_CPU.set(plan[slot])
        opt_token = _OPTIONS.set({})
        try:
            for b, group, k in todo[slot::a.jobs]:
                root, keep_root = roots(group, k)
                first = (b, group, k) == tasks[0]
                _OPTIONS.set({"decode": a.decode, "determinism_check": a.determinism_check and first,
                              "clean_spp": a.clean_spp})
                result = None
                try:
                    admit_unit(w, h, a.frames)
                    sk = None if group == "legacy" else k  # W10S: seed arms render seeded scenes
                    result = run_one(b, root, w, h, a.frames, spp, table, seed=k, analyze=group != "gate", scene_seed=sk)
                    if a.determinism_check and first:
                        repeat_root = root / "determinism_repeat"
                        repeat = run_one(b, repeat_root, w, h, a.frames, spp, table, seed=k, scene_seed=sk)
                        same = (canon.dumps(result["row"]) == canon.dumps(repeat["row"]) and
                                determinism_fingerprint(root / b) == determinism_fingerprint(repeat_root / b))
                        keep_root.mkdir(parents=True, exist_ok=True)
                        (keep_root / "determinism.json").write_text(json.dumps({"identical": same}, sort_keys=True) + "\n")
                        if not same:
                            raise BenchError("E_DETERMINISM", "sampled scene differs", rc=5)
                    result.update(seed_group=group, run_dir=str(root / b))
                    if group == "gate":  # GATE: rendered now, analysed at the frozen T0 before its release
                        result["_roots"] = (root, keep_root)
                    else:
                        finish_unit(root, b, keep_root, result, "%s.%s.%d.%s" % (tier_rel, group, k, b))
                    completed.append((b, group, k, result, None))
                except errors as e:
                    if group != "gate" or result is None:
                        try:
                            finish_unit(root, b, keep_root)
                        except errors:
                            pass
                    completed.append((b, group, k, None, failure(b, group, k, e)))
        finally:
            _OPTIONS.reset(opt_token)
            _JOB_CPU.reset(token)
        return completed

    def fly(todo):
        if not todo:
            return {}
        with ThreadPoolExecutor(max_workers=a.jobs) as pool:
            flights = list(pool.map(lambda slot: flight(slot, todo), range(a.jobs)))
        return {(b, group, k): (r, e) for flight_results in flights for b, group, k, r, e in flight_results}

    # Pass 1 renders + analyses FIT/legacy only. GATE units are rendered after T0 is frozen (bounded RAM peak: no GATE
    # capture waits in the workspace for the FIT pass); they are analysed exactly once, at the frozen T0.
    fit_tasks = [t for t in tasks if t[1] != "gate"]
    gate_tasks = [t for t in tasks if t[1] == "gate"]
    indexed = fly(fit_tasks)
    for task in fit_tasks:
        result, failure_row = indexed[task]
        if failure_row is not None:
            failures.append(failure_row)
        else:
            results.append(result)

    def gate_pass(table2):
        """Render GATE arms in batches of <= jobs units, analyse each at table2 in task order, persist, release."""
        done, bad = [], []
        for i in range(0, len(gate_tasks), a.jobs):
            batch = gate_tasks[i:i + a.jobs]
            got = fly(batch)
            pending = [t for t in batch if got[t][1] is None]
            bad += [got[t][1] for t in batch if got[t][1] is not None]
            try:
                for b, group, k in pending:
                    r = got[(b, group, k)][0]
                    root, keep_root = r["_roots"]
                    r2 = dict(analyze_bench(b, r["run_dir"], spp, table2, seed=k), seed_group=group, run_dir=r["run_dir"])
                    finish_unit(root, b, keep_root, r2, "%s.%s.%d.%s" % (tier_rel, group, k, b))
                    r.pop("_roots")
                    done.append(r2)
            finally:  # an analysis error still releases every unit of the batch
                for t in pending:
                    r = got[t][0]
                    if "_roots" in r:
                        root, keep_root = r.pop("_roots")
                        finish_unit(root, t[0], keep_root)
        return done, bad

    renderer = a.renderer or "cycles-cpu-%dspp-box" % spp
    grid = next((r["grid"] for r in results if "grid" in r), "%dx%d" % (w, h))
    report = {"schema": "bvqa.run-report/v1", "grid": grid, "spp": spp, "frames": a.frames, "renderer": renderer,
              "benches": _slim(results), "failures": failures, "calibration": {}, "profile": None, "seeds": a.seed_sets,
              "chunk_frames": CHUNK_FRAMES, "clean_spp": a.clean_spp}
    if failures:
        return _incomplete(out, report, results, failures)
    if not results:
        report["rc"] = 3
        (out / "calib_report.json").write_text(json.dumps(report, sort_keys=True, indent=1) + "\n")
        (out / "SUMMARY.md").write_text(summary_md(report))
        print(json.dumps({"code": "E_RUN", "rc": 3, "benches": 0, "failures": 0}, sort_keys=True))
        return 3
    if a.seed_sets is not None:
        import inspect
        if "seeds" not in inspect.signature(calibrate.calibrate).parameters:
            return _write_refusal(out, report, "split_aware_calibrator_unavailable", [r for r in results if "row" in r])
    calib_kw = {"seeds": a.seed_sets} if a.seed_sets is not None else {}
    fit_results = [r for r in results if r["seed_group"] in ("fit", "legacy")]
    still = [sample for r in fit_results if r["bench"] == "B6" and r.get("still_q4") for sample in r["still_q4"]]
    b6_fit = [r for r in fit_results if r["bench"] == "B6" and r.get("still_q4")]
    if a.seed_sets is not None:  # the B6 still probe is bound to the FIT seeds and captures it came from
        calib_kw["still_meta"] = {"seeds": sorted({r["seed"] for r in b6_fit}),
                                  "capture_sha256": sorted({r["row"].get("capture_sha256") for r in b6_fit} - {None})}
    report["still_probe"] = "B6" if still else "AUSENTE"
    if not still:
        return _write_refusal(out, report, "still_probe_absent", fit_results)
    still_doc = {"samples_q4": still, "sha256": canon.sha256_bytes(canon.dumps({"s": still}).encode())}
    if "still_meta" in calib_kw:  # ADV-W9-02: the manifest alone re-binds the probe (calibrate.main) and is hashed into src
        still_doc["still_meta"] = calib_kw["still_meta"]
    (out / "still_manifest.json").write_text(json.dumps(still_doc, sort_keys=True))
    src = canon.sha256_bytes((out / "still_manifest.json").read_bytes())
    rc = 0
    try:
        t0 = fit_t0(fit_results, still)  # frozen BEFORE any GATE row is labelled or scored
        report["frozen_t0_q4"] = t0
        table2 = vparams.effective({"T0_Q4": t0})
        # ADV-W9-03: FIT rows stay the pass-1 rows T0 was frozen on; only GATE is scored, once, at t0
        gate_done, gate_bad = gate_pass(table2)
        if gate_bad:
            return _incomplete(out, report, results + gate_done, gate_bad)
        frozen = []
        for (b, group, k) in tasks:
            frozen.append(next(x for x in results + gate_done if (x["bench"], x["seed"], x["seed_group"]) == (b, k, group)))
        results = frozen
        report["benches"] = _slim(results)
        (out / "rows.jsonl").write_text("".join(canon.dumps(r["row"]) for r in results))
        profile, crep = calibrate.calibrate([r["row"] for r in results], still_doc["samples_q4"], "blender",
                                            renderer, grid, src, a.strict, **calib_kw)
        crep["two_pass"] = t0 != int(table["T0_Q4"])
        if profile is not None and int(crep["T0_Q4"]) != t0:
            profile = None  # refit at the frozen labels moved T0: the evaluated labels do not match the profile
            crep.update(passed=False, refusal="t0_not_frozen", frozen_t0_q4=t0)
        if a.seed_sets is None:
            # Legacy single-arm runs remain useful probes but cannot certify disjoint data.
            profile = None
            crep.update(passed=False, refusal="seed_partition_absent")
        report["calibration"] = crep
        if profile is not None:
            (out / "profile.json").write_bytes(canon.dumps(profile).encode())
            report["profile"] = "profile.json"
        else:
            rc = 5
    except VqaError as e:
        report["calibration"] = {"error": e.to_obj()}
        rc = e.rc
    except BenchError as e:
        report["calibration"] = {"error": {"code": e.code, "msg": e.msg}}
        rc = e.rc
    report["rc"] = rc
    (out / "calib_report.json").write_text(json.dumps(report, sort_keys=True, indent=1) + "\n")
    (out / "SUMMARY.md").write_text(summary_md(report))
    sys.stdout.write(json.dumps({"code": "OK" if rc == 0 else "E_RUN", "rc": rc, "benches": len(results),
                                 "failures": len(failures), "spp": spp}, sort_keys=True) + "\n")
    return rc


def _main(argv=None):
    try:
        a = parse_args(argv)
        benches, w, h = validate(a)
    except BenchError as e:
        sys.stdout.write(json.dumps({"code": e.code, "rc": e.rc, "msg": e.msg}, sort_keys=True) + "\n")
        return e.rc
    except SystemExit:
        return 2
    if a.dry_run:  # JW9c-1: the plain W10 CLI starts on the pinned wheel; nothing is written
        sys.stdout.write(json.dumps({"code": "DRY_RUN", "rc": 0, "benches": benches, "res": [w, h], "frames": a.frames,
                                     "spp": a.spp_tiers, "seeds": a.seed_sets, "jobs": a.jobs, "cpu_plan": a.cpu_plan,
                                     "clean_spp": a.clean_spp, "engine": _ENGINE[0], "engine_source": _ENGINE[1],
                                     "engine_origins": _pin.origins()}, sort_keys=True) + "\n")
        return 0
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tiers = a.spp_tiers
    run_id = a.run_id or "r%d-%x" % (os.getpid(), time.time_ns())
    try:
        ws = ramws.open_run(a.campaign, run_id, a.ram_quota_mb << 20, tool=RAM_TOOL)
    except ramws.RamError as e:
        sys.stdout.write(json.dumps({"code": e.code, "rc": 5, "msg": str(e)}, sort_keys=True) + "\n")
        return 5
    _Store.ws, _Store.run_id, _Store.build = ws, run_id, REPO / "build"
    _Store.keep_left, _Store.keep, _Store.units = a.keep_frames << 20, [], []
    _Store.argv_sha = hashlib.sha256(json.dumps(sys.argv[1:] if argv is None else list(argv)).encode()).hexdigest()
    try:
        rcs = [run_tier(a, benches, w, h, spp, out if len(tiers) == 1 else out / ("spp_%d" % spp)) for spp in tiers]
        with _Store.lock:
            ws.account()
            receipt = dict(ws.receipt(), units=len(_Store.units), kept=_Store.keep,
                           kept_truncated=any(k["kept_truncated"] for k in _Store.keep),
                           unit_peak_max_bytes=max([u["ram_peak_bytes"] for u in _Store.units] or [0]))
    finally:
        _Store.ws = None
        ws.close()
    # L9 receipt; volatile (peak, swap) by design, so it is kept out of rows.jsonl (L10)
    (out / "ram_receipt.json").write_text(json.dumps(receipt, sort_keys=True, indent=1) + "\n")
    if len(tiers) > 1:
        (out / "tiers.json").write_text(json.dumps({"tiers": tiers, "rc": rcs}, sort_keys=True) + "\n")
    return max(rcs)


def main(argv=None):
    """Run bounded cycles-cpu-<spp>spp-box flights and reap children on interruption."""
    _STOP.clear()
    if threading.current_thread() is not threading.main_thread():
        return _main(argv)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for sig in previous:
            signal.signal(sig, _stop_children)
        return _main(argv)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
