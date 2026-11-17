"""LIVE U5. Pass two captures and their independently recorded pins.

Run with python3 -I -B inside the campaign's capped scope. Each --checkpoint
takes RUN_DIR BLEND_SHA256 RECEIPT_SHA256 from an operator-run Blender capsule.
Use checkpoints expected to satisfy the nonblank predicate.
"""

import argparse
from pathlib import Path
import sys


SURFACE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SURFACE.parent), str(SURFACE.parents[1] / "blender-visual-qa")]

from blender_visual_qa.drive.journal import DriveError
from vqa3d_bridge import evaluate_capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", action="append", nargs=3,
                        metavar=("RUN_DIR", "BLEND_SHA256", "RECEIPT_SHA256"), required=True)
    args = parser.parse_args()
    if len(args.checkpoint) != 2 or len({row[1] for row in args.checkpoint}) != 2:
        parser.error("supply two distinct Blender checkpoints")
    failed = False
    for capture, blend_sha256, receipt_sha256 in args.checkpoint:
        pins = {"blend_sha256": blend_sha256, "receipt_sha256": receipt_sha256}
        try:
            result = evaluate_capture(Path(capture), **pins)
            repeated = evaluate_capture(Path(capture), **pins)
        except DriveError as exc:
            print(f"FAIL {blend_sha256} {exc.code} {exc.reason}", file=sys.stderr)
            failed = True
            continue
        passed = result == repeated and result.accepted
        print(f"{'PASS' if passed else 'FAIL'} {result.scene_digest} score_ppm={result.score} [MEASURED]")
        for predicate in result.predicates:
            print(f"  {predicate.render} frame_not_blank={predicate.frame_not_blank}")
        failed |= not passed
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
