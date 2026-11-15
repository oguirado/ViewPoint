"""DEV-only plug-in evaluation and deterministic leaderboard serialization."""
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from eval.gui_synth import prereg
from eval.gui_synth.gen import Src
from eval.gui_synth.labels import derive
from eval.gui_synth.score import aggregate
from visioncore.desc import luma_of_padded
from visioncore.grid import Grid
from visioncore.pipeline import analyze
from .plugins import REFERENCES, Variant, parse_variant
from .postpass import arrays

OUT = Path(__file__).parent
RESULTS = OUT / "variant_results.json"


def quick_selection(scenes, count):
    """Stable round-robin by kind; prefer small DEV scenes within each kind."""
    if any(s.get("split") != "DEV" or not s["scene_id"].startswith("DEV:") for s in scenes):
        raise ValueError("only DEV may be evaluated")
    if count is None:
        return scenes
    if count < 1 or count > len(scenes):
        raise ValueError("quick must be within DEV scene count")
    buckets = {}
    for scene in sorted(scenes, key=lambda s: (s["w"]*s["h"], s["scene_id"])):
        buckets.setdefault(scene["kind"], []).append(scene)
    chosen = []
    for i in range(len(scenes)):
        for kind in sorted(buckets):
            if i < len(buckets[kind]):
                chosen.append(buckets[kind][i])
                if len(chosen) == count:
                    return chosen
    raise AssertionError("unreachable selection")


def run_variants(scenes, specs, budget_ms):
    from .run import add_honesty, metrics_from_predictions, write_json
    scenes = quick_selection(scenes, None)
    specs = sorted(set(specs) | REFERENCES)
    for spec in specs:
        parse_variant(spec)
    records = {name: {"status": "OK", "rows": [], "false_relabels": 0,
                      "static_jitter_tile_frames": 0, "python_opcodes": 0,
                      "calls": 0, "error": None} for name in ["baseline", *specs]}
    diagnostics = {name: 0.0 for name in ["baseline", *specs]}
    for scene in scenes:
        grid = Grid(scene["w"], scene["h"])
        start = time.perf_counter()
        engine = analyze(Src(scene), workers=1, cpus=(2, 3, 4))
        diagnostics["baseline"] += time.perf_counter()-start
        tiles = np.stack(engine.tiles)
        events = derive(scene, grid)
        base = {k: tiles[k].astype(np.int64) for k in ("cls", "mv_dx", "mv_dy")}
        records["baseline"]["rows"].append(metrics_from_predictions(base, events, scene))
        # A failed module contributes no partial metrics to the leaderboard.
        adapters = {}
        try:
            for spec in specs:
                if records[spec]["status"] == "OK":
                    adapters[spec] = Variant(spec, grid, budget_ms)
            predictions = {s: {k: np.empty_like(v) for k,v in base.items()} for s in adapters}
            prev = None
            for frame in Src(scene):
                cur = np.ascontiguousarray(luma_of_padded(frame.rgb), dtype=np.uint8)
                for spec, adapter in adapters.items():
                    output = adapter.update(prev, cur, tiles[frame.i])
                    if output is None:
                        continue
                    for key in output:
                        predictions[spec][key][frame.i] = output[key]
                    if scene["kind"] in ("static_clean", "aa_jitter"):
                        records[spec]["false_relabels"] += int(((output["cls"] != base["cls"][frame.i]) & (output["cls"] >= 2)).sum())
                prev = cur
            for spec, adapter in adapters.items():
                rec = records[spec]
                rec["status"], rec["error"] = adapter.status, adapter.error
                rec["module_sha256"] = adapter.module_sha256
                rec["python_opcodes"] += adapter.ops
                rec["calls"] += adapter.frames
                diagnostics[spec] += adapter.seconds
                if adapter.status == "OK":
                    rec["rows"].append(metrics_from_predictions(predictions[spec], events, scene))
            if scene["kind"] in ("static_clean", "aa_jitter"):
                for rec in records.values():
                    rec["static_jitter_tile_frames"] += grid.N*scene["n"]
        finally:
            for adapter in adapters.values():
                adapter.close()
        print(json.dumps({"phase": "variants", "scene": scene["scene_id"]}), flush=True)
    result = {"schema": "vqa.v2eval-variants/v1", "split": "DEV", "claims": "no claim; DEV only",
              "scene_ids": [s["scene_id"] for s in scenes], "frame_budget_ms": budget_ms,
              "prereg_sha256": prereg.sha256(), "measurement_tag": "MEASURED",
              "harness_sha256": hashlib.sha256(b"".join(
                  (Path(__file__).parent/name).read_bytes()
                  for name in ("run.py", "plugins.py", "exploration.py", "postpass.py"))).hexdigest(),
              "cost_scope": "executed Python bytecodes in frame API calls and Python dependencies; NumPy/C internals excluded; baseline uninstrumented",
              "variants": {}}
    for name, rec in records.items():
        rows = rec.pop("rows")
        rec["metrics"] = add_honesty(aggregate(rows)) if rec["status"] == "OK" and rows else None
        # Exclude uninstrumented baseline costs rather than claiming zero work.
        if name == "baseline":
            rec["python_opcodes"] = rec["calls"] = None
        elif rec["status"] == "FAILED":
            rec["python_opcodes"] = None
        result["variants"][name] = rec
    # Remove scorer's unmeasured defaults from the persisted evidence.
    from .run import omit_scorer_default_timings
    omit_scorer_default_timings(result)
    write_json(RESULTS, result)
    write_json(OUT/"variant_diagnostics.json", {"tag": "DIAGNOSTIC", "seconds": {name: seconds if name == "baseline" or records[name]["calls"] else None
                                                for name, seconds in diagnostics.items()},
               "scope": "successful frame API calls only; failed calls unavailable; baseline engine analyze only",
               "warning": "VM wall time is not performance evidence"})
    return result


def leaderboard(data):
    """No clock/host values: identical evidence gives byte-identical output."""
    if data.get("split") != "DEV" or not data.get("scene_ids") or any(not s.startswith("DEV:") for s in data["scene_ids"]):
        raise ValueError("leaderboard requires nonempty DEV-only evidence")
    rows = []
    for name, rec in sorted(data["variants"].items()):
        metrics = rec["metrics"] if rec["status"] == "OK" else None
        den = rec["static_jitter_tile_frames"]
        row = {"variant": name, "status": rec["status"], "error": rec["error"],
               "reference": name == "baseline" or name in REFERENCES,
               "MOVED": metrics["per_class"]["MOVED"] if metrics else None,
               "FLICKER": metrics["per_class"]["FLICKER"] if metrics else None,
               "mv_exact": metrics["mv_exact"] if metrics else None,
               "false_relabels_static_jitter_ppm": rec["false_relabels"]*1000000//den if den and metrics else None,
               "silent_miss_ppm": metrics["honesty"]["silent_miss_ppm"] if metrics else None,
               "honesty_flags": metrics["honesty"]["class_honesty"] if metrics else None,
               "python_opcodes": rec["python_opcodes"], "frame_calls": rec["calls"],
               "module_sha256": rec.get("module_sha256")}
        rows.append(row)
    return {"schema": "vqa.v2eval-leaderboard/v1", "split": "DEV", "scene_ids": data["scene_ids"],
            "frame_budget_ms": data["frame_budget_ms"], "cost_scope": data["cost_scope"],
            "prereg_sha256": data.get("prereg_sha256"), "harness_sha256": data.get("harness_sha256"),
            "measurement_tag": "MEASURED",
            "diagnostic_wall_time": "variant_diagnostics.json (DIAGNOSTIC; VM time is not evidence)",
            "rows": rows}


def write_leaderboard(data, out=OUT):
    from .run import write_json
    board = leaderboard(data)
    write_json(out/"LEADERBOARD.json", board)
    lines = ["# DEV variant leaderboard", "", "No claim; DEV only. Cost counts Python operations; native work is opaque.",
             "DIAGNOSTIC wall time: [variant_diagnostics.json](variant_diagnostics.json). VM wall time is not evidence.", "",
             "| Variant | Status | MOVED recall / precision | FLICKER recall / precision | mv exact | False relabel ppm | Silent miss ppm | Python ops | Honesty flags |",
             "|---|---|---|---|---|---|---|---|---|"]
    def ratio(r):
        return r["fraction"] if r and r["defined"] else "AUSENTE"
    for r in board["rows"]:
        def pair(label):
            return " / ".join(ratio(r[label][k]) for k in ("recall", "precision")) if r[label] else "AUSENTE"
        def show(v):
            return "AUSENTE" if v is None else str(v)
        flags = json.dumps(r["honesty_flags"], sort_keys=True, separators=(",", ":"))
        lines.append(f'| {r["variant"]} | {r["status"]} | {pair("MOVED")} | {pair("FLICKER")} | {ratio(r["mv_exact"])} | {show(r["false_relabels_static_jitter_ppm"])} | {show(r["silent_miss_ppm"])} | {show(r["python_opcodes"])} | {flags} |')
    (out/"LEADERBOARD.md").write_text("\n".join(lines)+"\n")
    return board
