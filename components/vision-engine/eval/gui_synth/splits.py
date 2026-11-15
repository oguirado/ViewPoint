"""Seed partitions and capture-identity leak refusal (CLI refusal status 5)."""
import argparse
import json
from collections.abc import Mapping

RANGES = {"DEV": (0, 64), "GATE": (1_000_000, 1_000_032), "TEST": (2_000_000, 2_000_032)}
SPLITS = RANGES
REFUSAL_SPLIT = 5

# Velocities/distance are integer sixteenths of the resolution's grid size.
# fps is a structural axis; every tuned parameter value is split-disjoint.
PARAMETER_SETS = {
    "DEV": {
        "scroll_small": {"dy": [4, 8, 12, 20, 24]},
        "scroll_large": {"dy": [48, 96, 200, 400, 600]},
        "scroll_h": {"dx": [8, 24, 96]},
        "caret_move": {"dx": [4, 8, 12, 20, 24]},
        "typing": {"dx": [4, 8, 12, 20, 24]},
        "window_drag": {"dx": [4, 8, 20], "dy": [4, 8, 20], "box_divisor": [4]},
        "caret_blink": {"half_period_ms": [250, 500, 1000], "fps": [30, 60]},
        "scroll_smooth": {"amplitude": [24]},
    },
    "GATE": {
        "scroll_small": {"dy": [6, 14, 22]},
        "scroll_large": {"dy": [36, 72, 144]},
        "scroll_h": {"dx": [6, 36, 72]},
        "caret_move": {"dx": [6, 14, 22]},
        "typing": {"dx": [6, 14, 22]},
        "window_drag": {"dx": [6, 14, 22], "dy": [6, 14, 22], "box_divisor": [5]},
        "caret_blink": {"half_period_ms": [300, 333, 367, 450, 750], "fps": [30, 60]},
        "scroll_smooth": {"amplitude": [36]},
    },
    "TEST": {
        "scroll_small": {"dy": [7, 13, 21]},
        "scroll_large": {"dy": [56, 152, 300]},
        "scroll_h": {"dx": [7, 56, 152]},
        "caret_move": {"dx": [7, 13, 21]},
        "typing": {"dx": [7, 13, 21]},
        "window_drag": {"dx": [7, 13, 21], "dy": [7, 13, 21], "box_divisor": [6]},
        "caret_blink": {"half_period_ms": [383, 417, 467, 550, 875], "fps": [30, 60]},
        "scroll_smooth": {"amplitude": [56]},
    },
}


def parameter_sets(split):
    """Return a detached copy so consumers cannot mutate the split registry."""
    if split not in PARAMETER_SETS:
        raise SplitLeak("unknown split")
    return json.loads(json.dumps(PARAMETER_SETS[split]))


class SplitLeak(ValueError):
    code = "split_leak"
    rc = REFUSAL_SPLIT

    def __init__(self, detail):
        super().__init__("split_leak: " + str(detail))


def seeds(split):
    if split not in RANGES:
        raise SplitLeak("unknown split")
    return range(*RANGES[split])


def _hashes(value):
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return set().union(*(_hashes(v) for v in value.values())) if value else set()
    return set().union(*(_hashes(v) for v in value)) if value is not None else set()


def check(split, ks, purpose="evaluate", *, frame_shas=None, sha_tables=None, prereg_path=None):
    """Refuse leaked seeds, tuning, cross-split capture SHA overlap, or a stale seal.

    sha_tables maps split names to frame hashes (or scene-id -> frame-hash lists).
    Repeated frames within a scene/split are permitted; overlap across splits is not.
    GATE/TEST always verify the seal, even when no captures are supplied yet.
    """
    allowed = seeds(split)
    if purpose not in ("evaluate", "eval", "score", "baseline", "tune", "generate", "test"):
        raise SplitLeak("unknown purpose")
    if purpose == "tune" and split != "DEV":
        raise SplitLeak("tuning is DEV only")
    ks = list(ks)
    if not ks or any(type(k) is not int or k not in allowed for k in ks):
        raise SplitLeak("seed outside " + split)
    tables = dict(sha_tables or {})
    if any(name not in RANGES for name in tables):
        raise SplitLeak("unknown capture split")
    if frame_shas is not None:
        tables[split] = [tables.get(split, []), frame_shas]
    seen = set()
    for name in sorted(tables):
        current = _hashes(tables[name])
        if seen.intersection(current):
            raise SplitLeak("capture-sha overlap across splits")
        seen.update(current)
    if split != "DEV" or prereg_path is not None:
        from . import prereg
        try:
            prereg.verify(prereg_path)
        except prereg.PreregError as exc:
            raise SplitLeak(str(exc)) from exc
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=tuple(RANGES), required=True)
    parser.add_argument("--ks", type=int, nargs="+", required=True)
    parser.add_argument("--purpose", default="evaluate")
    args = parser.parse_args(argv)
    try:
        check(args.split, args.ks, args.purpose)
    except (SplitLeak, ValueError, OSError) as exc:
        print(json.dumps({"error": "split_leak", "detail": str(exc)}))
        return REFUSAL_SPLIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
