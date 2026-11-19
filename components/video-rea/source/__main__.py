# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import sys

from .ingest import ingest, ingest_timeline_files


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import a recorded capsule.")
    parser.add_argument("file")
    parser.add_argument("--out", required=True, help="fresh directory or exact resume target")
    parser.add_argument("--sample", type=int, default=200, metavar="PERIOD_MS")
    parser.add_argument("--resume", action="store_true", help="replay and verify existing artifacts")
    parser.add_argument("--timeline-sha256", help="independent owner digest of a sealed timeline")
    parser.add_argument("--frame-files", nargs="+", metavar="PNG", help="ordered owner PNG exports")
    args = parser.parse_args(argv)
    if bool(args.timeline_sha256) != bool(args.frame_files):
        parser.error("timeline input requires both --timeline-sha256 and --frame-files")
    options = dict(sample_ms=args.sample, resume=args.resume)
    try:
        if args.frame_files:
            ingest_timeline_files(args.file, args.out, expected_sha256=args.timeline_sha256,
                                  files=args.frame_files, **options)
        else:
            ingest(args.file, args.out, **options)
    except (OSError, ValueError) as exc:
        print(f"import refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
