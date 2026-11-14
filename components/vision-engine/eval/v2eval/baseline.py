"""Read only the DEV object in the mixed sealed baseline result stream."""
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "eval/results/gui_synth/v1_baseline.json.gz"


def load_dev(path=BASELINE):
    # Ignore all preceding fields; never decode another split's data.
    with gzip.open(path, "rt", encoding="utf-8") as source:
        for line in source:
            if line.strip() == '"split_results": {':
                break
        else:
            raise ValueError("sealed baseline has no split_results")
        line = next(source)
        if line.strip() != '"DEV": {':
            raise ValueError("DEV must be the first split object")
        chunks, depth, quoted, escaped = ["{"], 1, False, False
        for line in source:
            for i, char in enumerate(line):
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char in "[{":
                    depth += 1
                elif char in "]}":
                    depth -= 1
                    if depth == 0:
                        chunks.append(line[:i+1])
                        result = json.loads("".join(chunks))
                        if any(not sid.startswith("DEV:") for sid in result["scene_units"]):
                            raise ValueError("foreign scene in DEV baseline")
                        return result
            chunks.append(line)
    raise ValueError("truncated DEV baseline")


def selection(dev):
    from eval.gui_synth.scenes import KINDS, build
    remaining = set(dev["scene_units"])
    scenes = []
    for w, h in ((320, 180), (1920, 1080)):
        for kind in KINDS:
            for k in range(64):
                scene = build(kind, k, w=w, h=h, n=120, split="DEV")
                if scene["scene_id"] in remaining:
                    scenes.append(scene)
                    remaining.remove(scene["scene_id"])
    if remaining:
        raise ValueError("cannot reproduce DEV scene identities: " + repr(sorted(remaining)[:3]))
    return scenes


def assert_match(actual, expected):
    for key in ("confusion", "confusion_v1_expected", "mv_correct", "mv_total",
                "fp_count", "fp_tile_frames", "frames", "tile_frames", "scenes"):
        if actual[key] != expected[key]:
            raise ValueError("DEV baseline mismatch: " + key)
    for kind, row in expected["per_kind"].items():
        for key in ("confusion", "mv_correct", "mv_total", "fp_count", "fp_tile_frames"):
            if actual["per_kind"][kind][key] != row[key]:
                raise ValueError("DEV baseline mismatch: " + kind + "/" + key)
    # Timings differ on shared CPUs. Every other scene metric must agree.
    for sid, row in expected["scene_units"].items():
        for key, value in row["metrics"].items():
            if "ms_per_frame" not in key and actual["scene_units"][sid]["metrics"][key] != value:
                raise ValueError("DEV baseline mismatch: " + sid + "/" + key)
    return True
