"""Crash-safe, resumable CLI for V2EVAL2 DEV variant evaluation.

Usage:
  taskset -c 2-4 python3 -m eval.v2eval.run_incremental \
    --variant API=MODULE [--variant API=MODULE ...] \
    [--quick N] [--frame-budget-ms MS] [--out DIR] [--resume]
"""
import argparse
import math
import os
from pathlib import Path

from eval.gui_synth import prereg
from .baseline import load_dev, selection
from .exploration import OUT, quick_selection
from .incremental import run_variants_incremental
from .plugins import parse_variant
from .run import SCRATCH, verify_source_seal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", action="append", default=[], metavar="API=MODULE")
    parser.add_argument("--quick", type=int, metavar="N")
    parser.add_argument(
        "--frame-budget-ms",
        type=float,
        default=1000,
        help="per-frame and initialization deadline; failures are reported",
    )
    parser.add_argument("--out", type=Path, default=OUT, metavar="DIR")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    for spec in args.variant:
        parse_variant(spec)
    if not math.isfinite(args.frame_budget_ms) or args.frame_budget_ms <= 0:
        parser.error("--frame-budget-ms must be positive and finite")
    if not os.sched_getaffinity(0) <= {2, 3, 4}:
        raise ValueError("invoke with taskset -c 2-4")

    SCRATCH.mkdir(parents=True, exist_ok=True)
    dev = load_dev()
    sources = verify_source_seal(dev)
    scenes = selection(dev)
    chosen = quick_selection(scenes, args.quick)
    result = run_variants_incremental(
        chosen,
        args.variant,
        args.frame_budget_ms,
        out=args.out,
        resume=args.resume,
    )
    if sources != prereg.source_hashes():
        raise ValueError("sealed benchmark changed during execution")
    return result


if __name__ == "__main__":
    main()
