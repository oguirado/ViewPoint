"""python3 -m judge --rt RT : print the readiness of every intent file under RT/intents (read-only)."""
import argparse
import glob
import json
import os
import sys

from .judge import History, score_intent


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rt", default=os.environ.get("VP_SWARM_RT", "/dev/shm/vqa-cu/ref"))
    ap.add_argument("--head", type=int, default=None, help="head frame (default: read from events.jsonl)")
    a = ap.parse_args(argv)

    def load(name, default):
        try:
            with open(os.path.join(a.rt, name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    h = History.from_events(os.path.join(a.rt, "events.jsonl"))
    head = h.head_frame if a.head is None else a.head
    claims, advice = load("claims.json", {}), load("judge_advice.json", None)
    for path in sorted(glob.glob(os.path.join(a.rt, "intents", "*", "*.json"))):
        try:
            with open(path) as f:
                it = json.load(f)
        except (OSError, ValueError):
            it = None
        r = score_intent(it, head, h, claims, advice=advice)
        r["file"] = os.path.relpath(path, a.rt)
        print(json.dumps(r, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
