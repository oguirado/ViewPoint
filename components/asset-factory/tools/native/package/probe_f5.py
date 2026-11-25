"""Exercise the original pinned F5 refusal functions without rendering."""
import argparse
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root))
from tools.render import pipeline, presentation_mux

for module in (pipeline, presentation_mux):
    if Path(module.__file__).resolve().parent != root / "tools/render":
        raise RuntimeError("F5 module outside staged runtime")

SETTINGS = dict(start=1, end=1, references=[], samples=16, reference_samples=32,
                resolution_percentage=100, seed=0, timeout_s=60)


def validate_request(device, sequencer_active):
    pipeline.validate_settings(device=device, **SETTINGS)
    pipeline.validate_sequencer(sequencer_active)


def inspect(device, sequencer_active):
    try:
        validate_request(device, sequencer_active)
    except pipeline.Refusal as exc:
        return {"rc": exc.rc, "reason": exc.reason}
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--sequencer-active", action="store_true")
    args = parser.parse_args()
    print(json.dumps({
        "requested": inspect(args.device, args.sequencer_active),
        "CPU": inspect("CPU", False),
        "CUDA": inspect("CUDA", False),
        "sequencer_active": inspect("CPU", True),
    }, sort_keys=True))
