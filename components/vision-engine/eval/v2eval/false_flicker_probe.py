"""Read-only diagnostic explaining false baseline FLICKER tile-frames on DEV scenes."""
import argparse
import json
from pathlib import Path
import sys

# Ensure repository root is in sys.path
repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import numpy as np

from eval.gui_synth.gen import Src
from eval.gui_synth.labels import NAMES, derive
from eval.v2eval.baseline import load_dev, selection
from eval.v2eval.exploration import quick_selection
from visioncore.grid import Grid
from visioncore.pipeline import analyze

C_FLICKER = 4  # Class 4 is FLICKER in visioncore / gui_synth
CAUSE_BUCKETS = ("moved_large_displacement", "moved_small", "changed_texture", "other")


def analyze_false_flicker(scenes, analyze_fn=analyze):
    """Explain baseline FLICKER predictions whose truth class is not FLICKER."""
    results_by_kind = {}

    summary_truth_class = {name: 0 for name in NAMES}
    summary_cause_bucket = {bucket: 0 for bucket in CAUSE_BUCKETS}
    summary_total_flicker = 0

    for scene in scenes:
        kind = scene["kind"]
        if kind not in results_by_kind:
            results_by_kind[kind] = {
                "counts_by_truth_class": {name: 0 for name in NAMES},
                "counts_by_cause_bucket": {bucket: 0 for bucket in CAUSE_BUCKETS},
                "total_false_flicker_tile_frames": 0,
                "records": [],
            }

        kind_data = results_by_kind[kind]
        grid = Grid(scene["w"], scene["h"])
        engine = analyze_fn(Src(scene), workers=1, cpus=(2, 3, 4))
        events = derive(scene, grid)

        n_frames = scene["n"]
        n_tiles = grid.N
        s = grid.s
        gw = grid.gw
        gh = grid.gh

        truth_cls = np.zeros((n_frames, n_tiles), dtype=np.uint8)
        truth_mv = {}

        for e in events:
            f0, f1 = e["f0"], e["f1"]
            ideal = e["ideal"]
            mv = e.get("mv")
            for f in range(f0, f1):
                for t in e["tiles"]:
                    truth_cls[f, t] = ideal
                    if ideal == 3 and mv is not None:
                        truth_mv[(f, t)] = tuple(mv)

        for f in range(n_frames):
            tiles_f = engine.tiles[f]
            if isinstance(tiles_f, dict) and "cls" in tiles_f:
                cls_array = np.asarray(tiles_f["cls"])
            elif hasattr(tiles_f, "dtype") and tiles_f.dtype.names and "cls" in tiles_f.dtype.names:
                cls_array = tiles_f["cls"]
            else:
                cls_array = np.asarray(tiles_f)

            global_pub = bool(engine.frame_rows[f]["global_mv"] is not None)

            # Filter once, before records or any per-kind/summary aggregate.
            # A correct FLICKER prediction has no false-positive cause bucket.
            flicker_indices = np.flatnonzero(
                (cls_array == C_FLICKER) & (truth_cls[f] != C_FLICKER))
            for t in flicker_indices:
                t = int(t)
                tc_int = int(truth_cls[f, t])
                tc_name = NAMES[tc_int]
                tx, ty = int(grid.tx[t]), int(grid.ty[t])
                is_edge = bool(tx == 0 or ty == 0 or tx == gw - 1 or ty == gh - 1)

                mv_vec = None
                if tc_name == "MOVED":
                    mv_vec = truth_mv.get((f, t), (0, 0))
                    dx, dy = mv_vec
                    if max(abs(dx), abs(dy)) >= s:
                        bucket = "moved_large_displacement"
                    else:
                        bucket = "moved_small"
                elif tc_name == "CHANGED":
                    bucket = "changed_texture"
                else:
                    bucket = "other"

                record = {
                    "scene_id": scene["scene_id"],
                    "frame": f,
                    "tile": t,
                    "tile_pos": [tx, ty],
                    "truth_class": tc_name,
                    "global_vector_published": global_pub,
                    "truth_mv": list(mv_vec) if mv_vec is not None else None,
                    "is_edge": is_edge,
                    "cause_bucket": bucket,
                }

                kind_data["records"].append(record)
                kind_data["counts_by_truth_class"][tc_name] += 1
                kind_data["counts_by_cause_bucket"][bucket] += 1
                kind_data["total_false_flicker_tile_frames"] += 1

                summary_truth_class[tc_name] += 1
                summary_cause_bucket[bucket] += 1
                summary_total_flicker += 1

    output = {
        "schema": "vqa.false_flicker_probe/v1",
        "split": "DEV",
        "measurement_tag": "VM",
        "kinds": results_by_kind,
        "summary": {
            "counts_by_truth_class": summary_truth_class,
            "counts_by_cause_bucket": summary_cause_bucket,
            "total_false_flicker_tile_frames": summary_total_flicker,
        },
    }
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", type=int, default=None, metavar="N", help="Select first N DEV scenes using quick_selection")
    parser.add_argument("--kinds", type=str, default=None, help="Comma-separated kind filter (e.g., scroll_h,canvas_anim)")
    args = parser.parse_args(argv)

    dev = load_dev()
    scenes = selection(dev)

    if args.kinds:
        kinds_set = {k.strip() for k in args.kinds.split(",") if k.strip()}
        scenes = [s for s in scenes if s["kind"] in kinds_set]

    if args.quick is not None:
        scenes = quick_selection(scenes, args.quick)

    result = analyze_false_flicker(scenes)
    json_str = json.dumps(result, sort_keys=True, indent=2)
    print(json_str)


if __name__ == "__main__":
    main()
