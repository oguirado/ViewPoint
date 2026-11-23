"""vc_* predicates over vision.json (vqa.vision/v1). Advisory in mode 'shadow', required in 'on'. stdlib only."""
import json
import hashlib
import os

from .harness import CANNOT_SEE, FAIL, PASS

VISION_SCHEMA = "vqa.vision/v1"
MODES = ("shadow", "on")
RUN_DIR_NAME = "vision"


class VisionError(Exception):
    pass


def load_vision(run_dir):
    path = os.path.join(run_dir, RUN_DIR_NAME, "vision.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != VISION_SCHEMA:
        return None
    if not isinstance(data.get("frames"), list) or not isinstance(data.get("regions"), list):
        # visioncore stores frame/region records in digest-bound sidecars.
        if not isinstance(data.get("files"), dict):
            return None
        for key, filename in (("frames", "frames.jsonl"), ("regions", "regions.jsonl")):
            try:
                with open(os.path.join(run_dir, RUN_DIR_NAME, filename), "rb") as fh:
                    raw = fh.read()
                if hashlib.sha256(raw).hexdigest() != data["files"][filename]:
                    return None
                data[key] = [json.loads(line) for line in raw.splitlines() if line.strip()]
            except (OSError, ValueError, KeyError, TypeError):
                return None
    return data


def _longest_run(frames):
    best = run = 0
    prev = None
    for f in sorted(set(frames)):
        run = run + 1 if prev is not None and f == prev + 1 else 1
        best = max(best, run)
        prev = f
    return best


def _count(p, name, default, lo):
    v = p.get(name, default)
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise VisionError("parameter " + name + " must be an integer >= " + str(lo))
    return v


def _cls(vision, params, mode, cls, reason, default_frames, consecutive, thresholds):
    if mode not in MODES:
        raise VisionError("mode")
    adv = mode == "shadow"
    min_tiles = _count(params, "min_tiles", 1, 1)
    min_frames = _count(params, "min_frames", default_frames, 1)
    if thresholds and cls in thresholds:   # profile row keyed (blender, renderer, grid) overrides defaults
        min_frames = _count(thresholds, cls, default_frames, 1)
    regs = [r for r in (vision or {}).get("regions", []) if isinstance(r, dict) and r.get("cls") == cls
            and type(r.get("frame")) is int and type(r.get("n_tiles")) is int and r["n_tiles"] >= min_tiles]
    frames = [r["frame"] for r in regs]
    score = _longest_run(frames) if consecutive else len(set(frames))
    metrics = {"min_tiles": min_tiles, "min_frames": min_frames, "frames_hit": score, "advisory": int(adv)}
    if vision is None:
        return CANNOT_SEE, "vision_json_missing", metrics
    hit = score >= min_frames
    return (FAIL if hit else PASS), (reason if hit else "no_" + reason), metrics


# Defaults are placeholders until the BTRUTH profile (vqa.profile/v1) supplies thresholds.
def vc_flicker(vision, params=None, mode="shadow", thresholds=None):
    return _cls(vision, params or {}, mode, "FLICKER", "vc_flicker", 3, False, thresholds)


def vc_frozen_subregion(vision, params=None, mode="shadow", thresholds=None):
    return _cls(vision, params or {}, mode, "FROZEN", "vc_frozen_subregion", 8, True, thresholds)


def vc_slow_drift(vision, params=None, mode="shadow", thresholds=None):
    return _cls(vision, params or {}, mode, "DRIFT", "vc_slow_drift", 1, False, thresholds)


PREDICATES = {"vc_flicker": vc_flicker, "vc_frozen_subregion": vc_frozen_subregion, "vc_slow_drift": vc_slow_drift}


def evaluate_vision(run_dir, predicate, params=None, mode="shadow", thresholds=None):
    if predicate not in PREDICATES:
        raise VisionError("unknown predicate " + predicate)
    return PREDICATES[predicate](load_vision(run_dir), params, mode, thresholds)


def effective_verdict(result):
    """Shadow mode never changes the v1.0 verdict: FAIL is reported as ADVISORY."""
    verdict, _reason, metrics = result
    return "ADVISORY" if verdict == FAIL and metrics.get("advisory") == 1 else verdict
