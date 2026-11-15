"""Reproduce sealed DEV, then score RAM-only prototype claims; never unseal TEST.

Usage: taskset -c 2-4 python3 -m eval.v2eval.run baseline|postpasses|report
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import time

import numpy as np
from eval.gui_synth import prereg
from eval.gui_synth.gen import Src
from eval.gui_synth.labels import derive
from eval.gui_synth.score import aggregate, score
from eval.honesty.metrics import class_honesty, silent_miss_ppm
from visioncore.pipeline import analyze
from .baseline import ROOT, assert_match, load_dev, selection
from .native import SCRATCH, library
from .postpass import PostPass, VARIANTS

OUT = Path(__file__).parent


def implementation_digest():
    digest = hashlib.sha256()
    for name in ("postpass.py", "native.py", "exact.c"):
        digest.update((OUT/name).read_bytes())
    for path in sorted((ROOT/"eval").glob("v2proto_*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def verify_source_seal(dev):
    """Check pinned source metadata without opening mixed result evidence."""
    if prereg.sha256() != dev["prereg_sha"]:
        raise ValueError("current seal differs from baseline DEV")
    sealed = json.loads(prereg.DEFAULT_PATH.read_text())
    current = prereg.source_hashes()
    if sealed["sources"] != current or sealed["sources_tree_sha256"] != prereg.tree_sha(current):
        raise ValueError("benchmark sources differ from sealed P1")
    return current


def write_json(path, data):
    path.write_text(json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + "\n")


def identity(scene):
    return hashlib.sha256(scene["scene_id"].encode()).hexdigest()[:24]


def pin_worker():
    slot = (multiprocessing.current_process()._identity[0] - 1) % 3
    os.sched_setaffinity(0, {2 + slot})


def lean(row):
    for key in ("legacy", "judge_view", "hypothesis_findings", "frame_shas"):
        row.pop(key, None)
    return row


def baseline_scene(scene):
    start = time.perf_counter()
    engine = analyze(Src(scene), workers=1, cpus=(2, 3, 4))
    elapsed = (time.perf_counter() - start)*1000
    tiles = np.stack(engine.tiles)
    events = derive(scene, engine.grid)
    row = lean(score(tiles["cls"], tiles["mv_dx"], tiles["mv_dy"], events,
                     scene=scene, engine_ms=elapsed))
    row["engine_tiles_sha256"] = hashlib.sha256(tiles.tobytes()).hexdigest()
    row["frames_sha256"] = engine.frames_sha256
    with (SCRATCH / (identity(scene) + ".tiles")).open("wb") as cache:
        np.save(cache, tiles, allow_pickle=False)
    write_json(SCRATCH / (identity(scene) + ".baseline.json"), row)
    return row


def metrics_from_predictions(predictions, events, scene):
    return lean(score(*(predictions[k] for k in ("cls", "mv_dx", "mv_dy")),
                      events, scene=scene))


def postpass_scene(scene):
    from visioncore.grid import Grid
    library()
    path = SCRATCH / (identity(scene) + ".tiles")
    tiles = np.load(path, allow_pickle=False)
    baseline_row = json.loads((SCRATCH / (identity(scene) + ".baseline.json")).read_text())
    if hashlib.sha256(tiles.tobytes()).hexdigest() != baseline_row["engine_tiles_sha256"]:
        raise ValueError("cached engine tiles corrupted")
    grid = Grid(scene["w"], scene["h"])
    adapter = PostPass(grid)
    predictions = {name: {k: np.empty((scene["n"], grid.N), dtype=np.int64)
                          for k in ("cls", "mv_dx", "mv_dy")} for name in VARIANTS[1:]}
    claim_counts = {name: 0 for name in VARIANTS[1:4]}
    relabels = {name: 0 for name in VARIANTS[1:]}
    false_relabels = {name: 0 for name in VARIANTS[1:]}
    conflicts = 0
    for frame in Src(scene):
        output, claims = adapter.update(tiles[frame.i], frame.rgb, frame_key=frame.sha256)
        for name in VARIANTS[1:]:
            for k in predictions[name]:
                predictions[name][k][frame.i] = output[name][k]
            changed = output[name]["cls"] != tiles[frame.i]["cls"]
            relabels[name] += int(changed.sum())
            if scene["kind"] in ("static_clean", "aa_jitter"):
                false_relabels[name] += int((changed & (output[name]["cls"] >= 2)).sum())
            union = np.logical_or.reduce([v["mask"] for v in claims.values()]) if name == "combined" else claims[name]["mask"]
            for field in predictions[name]:
                if not np.array_equal(output[name][field][~union], output["baseline"][field][~union]):
                    raise AssertionError("postpass changed an unclaimed tile")
        for name in claim_counts:
            claim_counts[name] += int(claims[name]["mask"].sum())
        conflicts += int((sum(c["mask"].astype(np.int8) for c in claims.values()) > 1).sum())
    events = derive(scene, grid)
    rows = {name: metrics_from_predictions(predictions[name], events, scene) for name in VARIANTS[1:]}
    row = {"scene_id": scene["scene_id"], "kind": scene["kind"],
           "implementation_sha256": implementation_digest(),
           "resolution": [scene["w"], scene["h"]], "n": scene["n"],
           "param_bucket": scene["param_bucket"], "variants": rows,
           "claim_counts": claim_counts, "relabels": relabels,
           "false_relabels": false_relabels, "conflicts": conflicts,
           "diagnostic_seconds": adapter.wall_seconds,
           "calls": adapter.calls, "cache_hits": adapter.cache_hits}
    write_json(SCRATCH / (identity(scene) + ".postpass.json"), row)
    return row


def run_parallel(function, scenes, phase):
    rows = []
    with ProcessPoolExecutor(max_workers=3, initializer=pin_worker) as pool:
        futures = {pool.submit(function, scene): scene for scene in scenes}
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(json.dumps({"phase": phase, "done": len(rows), "total": len(scenes),
                              "scene": futures[future]["scene_id"]}), flush=True)
    return rows


def add_honesty(row):
    c = row["confusion"]
    support = sum(sum(c[k]) for k in (2, 3, 4))
    row["honesty"] = {"silent_miss_ppm": silent_miss_ppm(c) if support else None,
                      "real_change_support": support, "class_honesty": class_honesty(c)}
    for child in row.get("per_kind", {}).values():
        add_honesty(child)
    return row


def omit_scorer_default_timings(value):
    """Zero defaults in the scorer are not measured postpass/engine costs."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("engine_ms", "generator_ms") or "ms_per_frame" in key:
                value[key] = None
            else:
                omit_scorer_default_timings(child)
    elif isinstance(value, list):
        for child in value:
            omit_scorer_default_timings(child)


def report(dev, scenes):
    baseline_rows = [json.loads((SCRATCH/(identity(s)+".baseline.json")).read_text()) for s in scenes]
    baseline = aggregate(baseline_rows)
    assert_match(baseline, dev)
    data = [json.loads((SCRATCH/(identity(s)+".postpass.json")).read_text()) for s in scenes]
    if any(r["implementation_sha256"] != implementation_digest() for r in data):
        raise ValueError("postpass results are stale")
    variants = {"baseline": add_honesty(baseline)}
    diagnostics = {}
    for name in VARIANTS[1:]:
        variants[name] = add_honesty(aggregate([r["variants"][name] for r in data]))
    targets = {}
    predicates = {
        "caret_blink_500ms_30fps": lambda s: s["kind"] == "caret_blink" and s["half_period_ms"] == 500 and s["fps"] == 30,
        "caret_blink_500ms_60fps": lambda s: s["kind"] == "caret_blink" and s["half_period_ms"] == 500 and s["fps"] == 60,
        "caret_move_actual_12px": lambda s: s["kind"] == "caret_move" and s["dx"] == 12,
        "caret_move_actual_12px_carrier": lambda s: s["kind"] == "caret_move" and s["dx"] == 12 and s["caret_variant"] == "carrier",
        "caret_move_actual_12px_texture": lambda s: s["kind"] == "caret_move" and s["dx"] == 12 and s["caret_variant"] == "texture",
    }
    for label, predicate in predicates.items():
        indices = [i for i,s in enumerate(scenes) if predicate(s)]
        targets[label] = {"scenes": len(indices), "variants": {}}
        for name in VARIANTS:
            rr = [baseline_rows[i] if name == "baseline" else data[i]["variants"][name] for i in indices]
            targets[label]["variants"][name] = add_honesty(aggregate(rr)) if rr else None
    by_resolution = {}
    for w,h in ((320,180),(1920,1080)):
        indices = [i for i,s in enumerate(scenes) if (s["w"],s["h"]) == (w,h)]
        by_resolution[str(w)+"x"+str(h)] = {name: add_honesty(aggregate([
            baseline_rows[i] if name == "baseline" else data[i]["variants"][name]
            for i in indices])) for name in VARIANTS}
    for name in VARIANTS[1:4]:
        rows = [r for r in data if r["resolution"] == [1920, 1080]]
        elapsed = sum(r["diagnostic_seconds"][name] for r in rows)
        count = sum(r["calls"][name] for r in rows)
        diagnostics[name] = {"tag": "DIAGNOSTIC", "implementation": "exact native kernels + content cache",
                             "scope": "prototype computation; spatial passes include luma conversion; common adapter output copies, source, engine and scorer excluded",
                             "wall_ms_per_frame": elapsed*1000/count, "frames": count,
                             "seconds": elapsed, "cache_hits": sum(r["cache_hits"] for r in rows)}
    false_relabels = {}
    for name in VARIANTS[1:]:
        numerator = sum(r["false_relabels"][name] for r in data)
        denominator = baseline["fp_tile_frames"]
        false_relabels[name] = {"count": numerator, "tile_frames": denominator,
                                "ppm": numerator*1000000//denominator}
    deltas = {}
    base_c = baseline["confusion"]
    for name in VARIANTS[1:]:
        c = variants[name]["confusion"]
        deltas[name] = {}
        for label, column in (("MOVED", 3), ("FLICKER", 4)):
            deltas[name][label+"_tp"] = c[column][column]-base_c[column][column]
            deltas[name][label+"_fp"] = sum(c[row][column]-base_c[row][column]
                                           for row in range(7) if row != column)
    sources = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted((ROOT/"eval").glob("v2proto_*.py"))}
    # Source and baseline SHA checks have validated this lineage without the
    # stock scorer's mixed-split evidence reader. Attach the proven seal.
    seal = prereg.sha256()
    collections = [variants, *by_resolution.values(),
                   *(target["variants"] for target in targets.values())]
    for collection in collections:
        for row in collection.values():
            if row is not None:
                row["prereg_sha"] = seal
    # Cost is reported exclusively in explicit DIAGNOSTIC records. Preserve
    # null for unavailable timing; do not publish scorer defaults as zero cost.
    omit_scorer_default_timings(variants)
    omit_scorer_default_timings(targets)
    omit_scorer_default_timings(by_resolution)
    result = {"schema": "vqa.v2eval/v1", "split": "DEV", "claims": "no claim; DEV only",
              "lane_status": "BLOCKED",
              "deviations": [
                  "Initial stock repository gate wrote temporary files under build/gate_disk outside the lane surface; later gates used a scratch checkout.",
                  "Initial baseline inspection exposed mixed DEV/GATE aggregate values; no held-out scenes were generated or scored.",
                  "Some early Python probes did not suppress bytecode writes.",
                  "Full-suite discovery was guarded against foreign writes, CPU affinity, held-out GUI generation and mixed-baseline reads; it was not a clean unrestricted pass."
              ],
              "measurement_tag": "MEASURED", "baseline_match": True,
              "sealed_sources_match": True,
              "prereg_sha256": prereg.sha256(), "prototype_sha256": sources,
              "postpass_implementation_sha256": implementation_digest(),
              "sealed_source_hashes": prereg.source_hashes(),
              "execution": {"cpus": [2, 3, 4], "workers": 3, "evaluation_frames_in_ram_only": True,
                            "scenes": len(scenes), "resolution_counts": {
                                str(w)+"x"+str(h): sum((s["w"],s["h"])==(w,h) for s in scenes)
                                for w,h in ((320,180),(1920,1080))}},
              "policy": {"global_max_d": "max(width,height)-1", "subtile_max_d": "s-1",
                         "flicker_max_half": 64, "flicker_hash": "S2 xh64",
                         "precedence": ["global_motion", "subtile", "flicker"],
                         "spatial_acceleration": "differential-tested exact native comparisons",
                         "zero_denominators": "undefined; class_honesty retains library contract"},
              "variants": variants, "false_relabels_static_jitter": false_relabels,
              "net_class_count_deltas": deltas,
              "defects": [
                  {"prototype": "global_motion", "at": "eval/v2proto_global_motion.py:119",
                   "issue": "Only the first surviving beam's local neighborhood is rescored; coarse pruning can lose the true vector.",
                   "effect": "Affects measured recall; the fixed 1080p scroll diagnostic misses its true vector."},
                  {"prototype": "global_motion", "at": "eval/v2proto_global_motion.py:136",
                   "issue": "The local uniqueness check misses distant exact aliases.",
                   "effect": "Periodic regression witness proves non-abstention; separate DEV impact AUSENTE, not isolated."},
                  {"prototype": "flicker", "at": "eval/v2proto_flicker.py:239",
                   "issue": "Active state emits FLICKER on held phases, following a four-half-period startup.",
                   "effect": "Adds UNCHANGED-to-FLICKER false labels and startup misses in the sealed window."},
                  {"prototype": "subtile", "at": "eval/v2proto_subtile.py:55",
                   "issue": "Partial edge blocks are skipped; a neighboring block match can claim an unverified exposed tile.",
                   "effect": "Observed on DEV scroll_small seed 0 frame 1 bottom tile: CHANGED becomes MOVED. False relabels are included in the reported counts."}
              ],
              "coverage_limits": [
                  "Actual 12px caret movement at 1080p: AUSENTE in sealed DEV.",
                  "Actual 12px narrow/textured caret cases have zero ideal MOVED support; carrier-motion scores are a separate proxy."
              ],
              "target_views": targets,
              "by_resolution": by_resolution,
              "baseline_scene_witnesses": {r["scene_id"]: {
                  "engine_tiles_sha256": r["engine_tiles_sha256"], "frames_sha256": r["frames_sha256"]}
                  for r in baseline_rows},
              "diagnostic_1080p": diagnostics,
              "scene_diagnostics": [{k:v for k,v in r.items() if k != "variants"} for r in data]}
    write_json(OUT/"results.json", result)
    print(json.dumps({"phase": "report", "baseline_match": True, "scenes": len(scenes)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("baseline", "postpasses", "report", "leaderboard"))
    parser.add_argument("--variant", action="append", default=[], metavar="API=MODULE")
    parser.add_argument("--quick", type=int, metavar="N")
    parser.add_argument("--frame-budget-ms", type=float, default=1000,
                        help="per-frame and initialization deadline; failures are reported")
    args = parser.parse_args()
    from .exploration import RESULTS, quick_selection, run_variants, write_leaderboard
    if args.phase == "leaderboard":
        write_leaderboard(json.loads(RESULTS.read_text()))
        return
    if (args.variant or args.quick is not None) and args.phase != "postpasses":
        parser.error("--variant and --quick require postpasses")
    if not os.sched_getaffinity(0) <= {2, 3, 4}:
        raise ValueError("invoke with taskset -c 2-4")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    dev = load_dev()
    sources = verify_source_seal(dev)
    scenes = selection(dev)
    if args.phase == "postpasses" and (args.variant or args.quick is not None):
        import math
        from .plugins import parse_variant
        for spec in args.variant:
            parse_variant(spec)
        if not math.isfinite(args.frame_budget_ms) or args.frame_budget_ms <= 0:
            parser.error("--frame-budget-ms must be positive and finite")
        chosen = quick_selection(scenes, args.quick)
        result = run_variants(chosen, args.variant, args.frame_budget_ms)
        if sources != prereg.source_hashes():
            raise ValueError("sealed benchmark changed during execution")
        write_leaderboard(result)
        return
    if args.phase == "baseline":
        rows = run_parallel(baseline_scene, scenes, "baseline")
        combined = aggregate(rows)
        assert_match(combined, dev)
        write_json(SCRATCH/"baseline_verified.json", {"baseline_match": True,
                   "scenes": len(scenes), "confusion": combined["confusion"],
                   "sources": sources, "prereg_sha256": prereg.sha256()})
    else:
        verified = json.loads((SCRATCH/"baseline_verified.json").read_text())
        if not verified["baseline_match"] or verified["sources"] != sources:
            raise ValueError("unproven or stale baseline")
        if args.phase == "postpasses":
            library().close()
            library.cache_clear()
            pending = []
            for scene in scenes:
                path = SCRATCH/(identity(scene)+".postpass.json")
                if not path.exists() or json.loads(path.read_text()).get("implementation_sha256") != implementation_digest():
                    pending.append(scene)
            run_parallel(postpass_scene, pending, "postpasses")
        else:
            report(dev, scenes)
    if sources != prereg.source_hashes():
        raise ValueError("sealed benchmark changed during execution")


if __name__ == "__main__":
    main()
