#!/usr/bin/env python3
"""P5 offline region-noise experiment; consumes persisted metadata and truth tiles only.

Usage: python3 tools/noise_rescore.py --run RUN/spp_4 --alpha .001 .01 .05 --out NEW

No render, refit of engine parameters, profile, pixels, or certification. The noise rule
never receives truth. Frozen truth is used only by the unchanged scorer and baseline
checks. Each alpha is applied independently to the ORIGINAL engine regions.

The pinned engine stores r_sum/r_max in min(255, q4 >> 2) display units. Transform
the FIT still samples into those same units, retaining the exact raw p99.9 rank and
R_q4 in the report. Do not treat a percentile as a proved bound: FIT tail counts,
estimated-mean and independence assumptions are reported explicitly.

Outside regions, UNCHANGED is the requested projection of original UNCHANGED/JITTER.
It preserves every scorer target; baseline confusion combines columns 0 and 1.
Mixed CHG islands lose the spatial assignment of CHANGED/FLICKER/DRIFT. Those tiles
remain unknown, are counted as ABSENT, and block the sweep instead of being painted
with the modal region class. A new output directory is the only write destination.
"""
import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blender_visual_qa.truth import score  # noqa: E402
from blender_visual_qa.truth.noise_rescore import (  # noqa: E402, F401
    EDGE_CEILING_PPM,
    FIELD_SAT_DISPLAY,
    NoiseRescoreError,
    REPORT_NAME,
    ROW_COMPARE,
    SCHEMA,
    UPSTREAM_KEYS,
    _absent_checks,
    _alpha,
    _baseline,
    _checks,
    _class,
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
    _region_stats,
    _rescore,
    _score_unit,
    _seed_set,
    _unit_path,
    decode_regions,
    fit_noise,
    render_md,
    should_demote,
)


def rescore(run, out, alphas, hard_ceiling=None, edge_ceiling_ppm=EDGE_CEILING_PPM):
    """Validate first, then sweep; never change a byte under run or an existing out."""
    return _rescore(run, out, alphas, hard_ceiling, edge_ceiling_ppm, fitter=fit_noise)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="persisted single-spp run (read only)")
    parser.add_argument("--alpha", required=True, type=float, nargs="+", help="distinct alpha values in (0,1)")
    parser.add_argument("--out", required=True, help="new directory outside the run")
    parser.add_argument("--hard-ceiling", type=float, default=None, help="r_max ceiling in display units; default fitted R")
    parser.add_argument("--edge-ceiling-ppm", type=int, default=EDGE_CEILING_PPM)
    args = parser.parse_args(argv)
    try:
        doc = rescore(args.run, args.out, args.alpha, args.hard_ceiling, args.edge_ceiling_ppm)
        b = doc["baseline"]
        print("ABSENT exact_class_tiles=%d mixed_dirty=%d ambiguous_clean=%d" % (
            b["class_absent_tiles"], b["mixed_class_tiles"], b["clean_class_absent_tiles"]))
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
