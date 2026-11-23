#!/usr/bin/env python3
"""Diagnose scene-specific static false positives from persisted unit evidence only.

Usage: python3 tools/static_fp_cause.py --run RUN --out NEW

For each (bench, seed) unit listed in rows.jsonl:
  - Loads ground-truth tile labels with score.read_truth_tiles.
  - Loads per-tile engine classes from vision/tile_cls.jsonl when present, else
    decodes regions via tools/noise_rescore.py decode_regions (unknown tiles are
    counted as ABSENT, never guessed).
  - Selects false dirty tiles: truth tl in score.FP_POP (STATIC=0, SHADE_WEAK=7)
    with a predicted dirty class (cls >= score.DIRTY_MIN).
  - Attributes each false dirty tile to exactly one cause in strict rule order:
      1. SHADE           : truth tl == SHADE_WEAK (7)
      2. EDGE_BLEED      : within 1 tile (Chebyshev) of a truth-dirty tile in the same frame
      3. GLOBAL_EXPOSURE : frame where static dirty fraction exceeds threshold and the
                           engine frame row shows a global luma or drift signal
      4. LIGHT_CHANGE    : scene journal or manifest lists a light or world change on that frame
      5. AA_JITTER       : static tile whose engine class flips across consecutive frames
                           with no truth change
      6. UNATTRIBUTED    : otherwise
  - Any cause whose required persisted field is absent is reported with reason ABSENT,
    never guessed. Never writes frames or images to disk.
"""
import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blender_visual_qa.truth import score  # noqa: E402
from blender_visual_qa.truth.static_fp_cause import (  # noqa: E402, F401
    ALL_CAUSES,
    CAUSE_AA_JITTER,
    CAUSE_ABSENT,
    CAUSE_EDGE_BLEED,
    CAUSE_GLOBAL_EXPOSURE,
    CAUSE_LIGHT_CHANGE,
    CAUSE_SHADE,
    CAUSE_UNATTRIBUTED,
    DIRTY_TL,
    GLOBAL_DRIFT_KEYS,
    GLOBAL_EXPOSURE_THRESHOLD_PPM,
    GLOBAL_LUMA_KEYS,
    REPORT_JSON,
    RULE_ORDER,
    SCHEMA,
    SUMMARY_MD,
    StaticFpCauseError,
    TOP_FRAMES_LIMIT,
    _aggregate_causes,
    _attribute_unit,
    _dilate_chebyshev_1,
    _fail,
    _grid,
    _has_signal_value,
    _inside,
    _inspect_global_exposure,
    _inspect_light_change,
    _int,
    _json,
    _jsonl,
    _locate_truth_path,
    _locate_unit_dir,
    _parse_cls_token,
    diagnose,
    load_tile_cls_jsonl,
    render_summary_md,
)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Read-only input run directory containing rows.jsonl")
    parser.add_argument("--out", required=True, help="New output directory outside --run")
    parser.add_argument(
        "--global-exposure-ppm",
        type=int,
        default=GLOBAL_EXPOSURE_THRESHOLD_PPM,
        help="Static dirty fraction threshold in ppm for GLOBAL_EXPOSURE (default: %(default)s)",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        report = diagnose(args.run, args.out, global_exposure_ppm=args.global_exposure_ppm)
    except StaticFpCauseError as exc:
        err = {"code": exc.code, "message": str(exc), "detail": exc.detail, "rc": exc.rc}
        sys.stdout.write(json.dumps(err, sort_keys=True) + "\n")
        return exc.rc
    except score.VqaError as exc:
        sys.stdout.write(json.dumps(exc.to_obj(), sort_keys=True) + "\n")
        return exc.rc
    sys.stdout.write(
        json.dumps(
            {
                "code": "OK",
                "rc": 0,
                "fp_tiles": report["fp_tiles"],
                "absent_tiles": report["absent_tiles"],
                "out": str(Path(args.out).resolve() / REPORT_JSON),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
