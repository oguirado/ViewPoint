#!/usr/bin/env python3
"""LIVE: project a synthetic handoff through an initialized test-wall connection."""

import argparse
import json
import os
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bus.push import ContextWallClient, push


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", type=Path, help="synthetic broker JSONL fixture")
    parser.add_argument("--room", required=True, help="fresh isolated test room")
    parser.add_argument("--inbox-agent", required=True)
    parser.add_argument("--expect-from", required=True, help="source microtask identifier")
    parser.add_argument("--expect-to", required=True, help="destination microtask identifier")
    parser.add_argument("--read-fd", required=True, type=int, help="initialized wall response pipe")
    parser.add_argument("--write-fd", required=True, type=int, help="initialized wall request pipe")
    args = parser.parse_args()
    with (args.events.open(encoding="utf-8") as events,
          os.fdopen(args.read_fd, "rb", closefd=False) as reader,
          os.fdopen(args.write_fd, "wb", closefd=False) as writer):
        summary = push((json.loads(line) for line in events), args.room,
                       ContextWallClient(reader, writer), inbox_agent=args.inbox_agent)
    print(json.dumps(summary, sort_keys=True))
    expected = (args.expect_from, args.expect_to)
    handoffs = {(item["from"], item["to"]) for item in summary["handoffs_read_back"]}
    return 0 if summary["calls"] and not summary["refused"] and expected in handoffs else 1


if __name__ == "__main__":
    raise SystemExit(main())
