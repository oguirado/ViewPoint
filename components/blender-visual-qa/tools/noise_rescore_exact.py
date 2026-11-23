#!/usr/bin/env python3
"""Exact offline region-noise rescore; fills mixed CHG region tiles (-1) from tile_cls.jsonl.

Usage: python3 tools/noise_rescore_exact.py --run RUN/spp_4 --alpha .001 .01 .05 --out NEW

Reuses tools/noise_rescore.py functions and fills tiles that decode_regions marks -1
from vision/tile_cls.jsonl when grid, frame ids, and per-frame raw-class sha256 agree,
failing with E_ABSENT otherwise, and reports absent_tiles.
"""
import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blender_visual_qa.truth import score  # noqa: E402
from blender_visual_qa.truth.noise_rescore_exact import (  # noqa: E402, F401
    EDGE_CEILING_PPM,
    NoiseRescoreError,
    REPORT_NAME,
    ROW_COMPARE,
    SCHEMA,
    UPSTREAM_KEYS,
    _absent_checks,
    _alpha,
    _baseline,
    _checks,
    _confusion,
    _fail,
    _finite,
    _grid,
    _hist,
    _inputs,
    _inside,
    _int,
    _json,
    _jsonl,
    _load_unit,
    _project_confusion,
    _recall_drops,
    _score_unit,
    _seed_set,
    _unit_path,
    decode_regions,
    fill_tile_classes,
    fit_noise,
    render_md,
    rescore,
    should_demote,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="persisted single-spp run with tile_cls.jsonl (read only)")
    parser.add_argument("--alpha", required=True, type=float, nargs="+", help="distinct alpha values in (0,1)")
    parser.add_argument("--out", required=True, help="new directory outside the run")
    parser.add_argument("--hard-ceiling", type=float, default=None, help="r_max ceiling in display units; default fitted R")
    parser.add_argument("--edge-ceiling-ppm", type=int, default=EDGE_CEILING_PPM)
    args = parser.parse_args(argv)
    try:
        doc = rescore(args.run, args.out, args.alpha, args.hard_ceiling, args.edge_ceiling_ppm)
        b = doc["baseline"]
        print("absent_tiles=%d exact_class_tiles=%d mixed_dirty=%d ambiguous_clean=%d" % (
            doc["absent_tiles"], b["class_absent_tiles"], b["mixed_class_tiles"], b["clean_class_absent_tiles"]))
        print(render_md(doc), end="")
        return 5 if doc["refused"] else 0
    except NoiseRescoreError as exc:
        print(json.dumps({"code": exc.code, "message": str(exc), **exc.detail}, sort_keys=True))
        return 5 if exc.code in ("E_ABSENT", "E_SPLIT", "E_BASELINE") else 2
    except (OSError, ValueError, KeyError, TypeError, score.VqaError) as exc:
        print(json.dumps({"code": "E_SCHEMA", "message": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
