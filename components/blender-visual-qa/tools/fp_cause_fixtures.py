#!/usr/bin/env python3
"""Synthetic static false-positive cause fixture generator.

Generates a persisted run tree layout readable by tools/noise_rescore.py
and blender_visual_qa.truth.score, plus expected_causes.json pinning
static false-positive cause claims (SHADE, EDGE_BLEED, GLOBAL_EXPOSURE,
AA_JITTER, UNATTRIBUTED).
"""
import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from blender_visual_qa.truth.fp_cause_fixtures import (  # noqa: E402, F401
    DIRTY_TL,
    FP_POP,
    SCHEMA_EXPECTED,
    classify_fp_tile,
    generate_fixtures,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output directory for synthetic run tree")
    parser.add_argument("--seed", type=int, default=42, help="random seed / run seed")
    args = parser.parse_args(argv)

    generate_fixtures(args.out, args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
