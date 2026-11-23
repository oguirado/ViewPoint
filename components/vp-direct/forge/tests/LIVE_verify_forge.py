"""LIVE readback of a sealed forge receipt from an operator-provisioned F1 run."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from forge import verify_receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal_directory", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    try:
        receipt = json.loads(args.receipt.read_bytes())
        verify_receipt(args.journal_directory, receipt)
    except (OSError, ValueError):
        print("BLOCKED_SOURCE", file=sys.stderr)
        return 1
    print(receipt["lineage"]["event_hash"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
