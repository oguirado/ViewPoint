"""RAM-only GUI benchmark runner. Frames never leave vadapt.ramws (refusal rc 5)."""
import argparse
import fcntl
import gzip
import json
import os
import time
import uuid
from pathlib import Path

from . import prereg, splits

RESULTS = prereg.ROOT / "eval/results/gui_synth"
TEST_LEDGER = RESULTS / "test_access.jsonl"
CPUS = tuple(range(5, 10))


def _pin_cpus():
    os.sched_setaffinity(0, CPUS)
    if os.sched_getaffinity(0) != set(CPUS):
        raise ValueError("benchmark requires CPUs 5-9")


def _test_access(split, unseal_test, engine_version, candidate, selection, ledger=None):
    if split != "TEST":
        return
    if engine_version != "v2" or not candidate or unseal_test != prereg.sha256():
        raise splits.SplitLeak("TEST requires v2 candidate and exact --unseal-test prereg SHA")
    path = Path(ledger or TEST_LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        out.seek(0)
        rows = [json.loads(line) for line in out if line.strip()]
        if any(row["candidate"] == candidate for row in rows):
            raise splits.SplitLeak("TEST already accessed for this v2 candidate")
        out.write(json.dumps({"schema": "vqa.gui_synth.test_access/v1", "candidate": candidate,
                              "prereg_sha256": unseal_test, "selection": selection}, sort_keys=True) + "\n")
        out.flush()
        os.fsync(out.fileno())


def _capture_tables(directory):
    """Read only benchmark result evidence, never frame files or arbitrary trees."""
    tables = {}
    directory = Path(directory)
    if not directory.exists():
        return tables
    for path in sorted((*directory.glob("*.json"), *directory.glob("*.json.gz"))):
        try:
            with (gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz"
                  else path.open(encoding="utf-8")) as source:
                data = json.load(source)
        except (OSError, ValueError, EOFError) as exc:
            raise splits.SplitLeak("unreadable capture evidence " + path.name) from exc
        if not isinstance(data, dict):
            continue
        captures = data.get("capture_shas", {})
        if not isinstance(captures, dict):
            raise splits.SplitLeak("invalid capture evidence " + path.name)
        for split, values in captures.items():
            tables.setdefault(split, []).append(values)
    return tables


def run_scene(scene, k=None, *, split="DEV", purpose="evaluate", workers=5, sha_tables=None,
              prereg_sha=None, labels_out=None, w=1920, h=1080, fps=None, n=120):
    """Render one scene in RAM, analyze it, score arrays, close all frame storage."""
    import numpy as np
    from vadapt.ramws import open_run, sweep
    from visioncore.pipeline import analyze
    from .gen import Src, to_ram
    from .labels import derive
    from .score import score
    if isinstance(scene, str):
        from .scenes import build
        scene = build(scene, k, w=w, h=h, fps=fps, n=n, split=split)
    if type(workers) is not int or not 1 <= workers <= 5:
        raise ValueError("workers must be 1..5")
    _pin_cpus()
    if scene.get("split") != split:
        raise splits.SplitLeak("scene split mismatch")
    splits.check(split, [scene["k"]], purpose)
    w, h, n = scene["w"], scene["h"], scene["n"]
    quota = w * h * 3 * n + (1 << 20)
    started = time.perf_counter()
    try:
        with open_run("P1", "scene-" + uuid.uuid4().hex, quota_bytes=quota, tool="gui_synth") as ws:
            rel, shas = to_ram(scene, ws)
            generator_ms = (time.perf_counter() - started) * 1000
            splits.check(split, [scene["k"]], purpose, frame_shas=shas, sha_tables=sha_tables)
            src = Src((scene, ws, rel))
            tick = time.perf_counter()
            engine = analyze(src, workers=workers, cpus=CPUS)
            engine_ms = (time.perf_counter() - tick) * 1000
            events = derive(scene, engine.grid)
            if labels_out is not None:
                for event in events:
                    labels_out.write(json.dumps(event, sort_keys=True) + "\n")
            result = score(np.stack([t["cls"] for t in engine.tiles]),
                           np.stack([t["mv_dx"] for t in engine.tiles]),
                           np.stack([t["mv_dy"] for t in engine.tiles]), events,
                           scene=scene, split=split, prereg_sha=prereg_sha,
                           frame_shas=shas, engine_ms=engine_ms, generator_ms=generator_ms)
            del src
            result["frame_shas"] = shas
            return result
    finally:
        sweep("gui_synth")


def execute(argv=None, *, split="DEV", kinds=None, ks=None, w=320, h=180, fps=None, n=120,
            backend="python", purpose="evaluate", workers=5, out=None, labels=None,
            unseal_test=None, engine_version="v1", candidate=None):
    if argv is not None:
        return main(argv)
    from .scenes import KINDS, build
    from .score import aggregate
    kinds = list(KINDS if kinds is None else kinds)
    ks = list(splits.seeds(split) if ks is None else ks)
    splits.check(split, ks, purpose)
    if backend != "python":
        raise ValueError("only the python backend is registered")
    if not kinds or len(set(kinds)) != len(kinds) or any(k not in KINDS for k in kinds):
        raise ValueError("unknown or duplicate scene kind")
    if any(type(x) is not int or x <= 0 for x in (w, h, n)):
        raise ValueError("resolution, fps and frame count must be positive integers")
    if not 1 <= workers <= 5:
        raise ValueError("workers must be 1..5")
    if fps is not None and (type(fps) is not int or fps <= 0):
        raise ValueError("fps must be a positive integer")
    if split != "DEV" and ((w, h) not in ((320, 180), (1920, 1080)) or fps not in (None, 30, 60) or n != 120):
        raise splits.SplitLeak("held-out runs require preregistered resolution/fps/frame count")
    if labels is not None and Path(labels).suffix != ".jsonl":
        raise ValueError("labels output must be JSONL")
    if out is not None and Path(out).suffix != ".json":
        raise ValueError("result output must be JSON")
    _pin_cpus()
    seal_sha = prereg.sha256() if prereg.DEFAULT_PATH.exists() else None
    captures = _capture_tables(RESULTS)
    if out is not None and Path(out).parent.resolve() != RESULTS.resolve():
        other = _capture_tables(Path(out).parent)
        for name, values in other.items():
            captures.setdefault(name, []).append(values)
    splits.check(split, ks, purpose, sha_tables=captures)
    _test_access(split, unseal_test, engine_version, candidate,
                 {"kinds": kinds, "ks": ks, "resolution": [w, h], "fps": fps, "n": n})
    rows, sha_table = [], {}
    sources_before = prereg.source_hashes()
    start = time.perf_counter()
    load1 = os.getloadavg()[0]
    sink = None
    try:
        if labels is not None:
            Path(labels).parent.mkdir(parents=True, exist_ok=True)
            sink = Path(labels).open("w", encoding="utf-8")
        for kind in kinds:
            for k in ks:
                scene = build(kind, k, w=w, h=h, fps=fps, n=n, split=split)
                row = run_scene(scene, split=split, purpose=purpose, workers=workers,
                                sha_tables=captures, prereg_sha=seal_sha, labels_out=sink)
                rows.append(row)
                sha_table[scene["scene_id"]] = row["frame_shas"]
                captures.setdefault(split, []).append(row["frame_shas"])
    finally:
        if sink is not None:
            sink.close()
    if prereg.source_hashes() != sources_before:
        raise prereg.PreregError("benchmark sources changed during generation/scoring")
    result = aggregate(rows)
    result.update({"split": split, "backend": backend, "engine_version": engine_version,
                   "prereg_sha256": seal_sha, "capture_shas": {split: sha_table},
                   "sha_table_sha256": prereg.table_sha(sha_table),
                   "sources_tree_sha256": prereg.tree_sha(sources_before),
                   "execution": {"workers": workers, "cpus": list(CPUS), "load1": load1,
                                 "wall_ms": (time.perf_counter() - start) * 1000,
                                 "resolution": [w, h], "fps": fps, "frames_per_scene": n}})
    if out is not None:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=tuple(splits.RANGES), required=True)
    parser.add_argument("--kinds", nargs="+")
    parser.add_argument("--ks", "--seeds", "--k", type=int, nargs="+")
    parser.add_argument("--res", default="320x180")
    parser.add_argument("--fps", type=int)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--backend", default="python")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--purpose", default="evaluate")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--unseal-test")
    parser.add_argument("--engine-version", choices=("v1", "v2"), default="v1")
    parser.add_argument("--candidate")
    args = parser.parse_args(argv)
    try:
        w, h = (int(x) for x in args.res.split("x"))
        result = execute(split=args.split, kinds=args.kinds, ks=args.ks, w=w, h=h,
                         fps=args.fps, n=args.frames, backend=args.backend,
                         purpose=args.purpose, workers=args.workers, out=args.out, labels=args.labels,
                         unseal_test=args.unseal_test, engine_version=args.engine_version, candidate=args.candidate)
        print(json.dumps({"ok": True, "out": str(args.out), "sha_table_sha256": result["sha_table_sha256"]}))
        return 0
    except (splits.SplitLeak, prereg.PreregError) as exc:
        print(json.dumps({"error": "split_leak", "detail": str(exc)}))
        return 5
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "E_GUI_RUN"), "detail": str(exc)}))
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
