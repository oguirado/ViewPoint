# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import sys

from source.records import canonical

from . import reconstruct


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reconstruct a synthetic game scene from W1 frames.")
    parser.add_argument("png_dir", help="W1 directory with the PNG manifest and clock")
    args = parser.parse_args(argv)
    try:
        data = canonical(reconstruct(args.png_dir, project_partial_solids=True))
    except (OSError, ValueError) as exc:
        print(f"scene reconstruction refused: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
