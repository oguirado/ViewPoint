"""Deterministic, RAM-only DEV evaluation for the common ``sf_`` API.

``evaluate(variants, *, per_kind=2, frames=8, size=(640, 360), kinds=None,
diagnostics=None)`` (also ``run``) accepts module names or module objects. Each
module must expose screen_fingerprint, fingerprint_distance, same_screen, and
the exact Python int SF_THRESHOLD. Distances must be nonnegative Python ints;
decisions must be exact bools equal to ``distance <= SF_THRESHOLD``.

Only seeds from splits.seeds('DEV') are used; per_kind cannot exceed 64. All
unordered frame pairs in each of the ten same-screen kinds contribute recall.
scroll_large and scroll_page have no same-screen label: their within-scene
decision rates appear separately. Every unordered pair of distinct scenes'
first frames contributes separation, including transition scenes, cross-kind
pairs and different k values. Per-kind separation counts each pair once for
each distinct endpoint kind; these rows therefore must not be summed. Rates
are integer parts per million (floor), with null for an empty denominator.

Frames are streamed from gen.render. An immutable bytes backing copy in RAM
makes the ndarray passed to each variant read-only, including attempts to
enable its write flag. Each call receives a fresh view; metadata mutation is a
contract failure and cannot alter the next variant's view. No frame, luma plane
or pixel buffer is written to disk.
Only current-scene fingerprints and all scenes' first fingerprints are retained.

Fingerprint size is len(fp) for exact bytes: native payload bytes, excluding
Python object overhead. For other deeply immutable fingerprints it is the
UTF-8 length of canonical tagged JSON. Supported composites are tuples,
namedtuples, frozensets and frozen dataclasses of None, bool, int, str, bytes or
those composites. Tags preserve types; byte atoms are hex; set elements are
sorted by canonical encoding; dataclasses include their qualified type and
field names. Sizes include tags for composites, are not heap sizes, and are
reported with encoding counts so unlike encodings are not mistaken for wire
size comparisons. Mutable or unsupported representations fail that variant.

Canonical metrics use sorted compact JSON, no floats or volatile timestamps.
Actual perf_counter_ns wall times are labelled VM and emitted as a separate
canonical JSON object to CLI stderr, or into the optional diagnostics mapping.
This separation is necessary: observed wall time cannot itself be identical
across runs. CLI --out contains only reproducible metrics; absent --out these
metrics go to stdout. Failure type and stage are in metrics; exception messages
and tracebacks, which may contain process-specific text, are in diagnostics.

Import, call and contract exceptions mark a variant FAILED, discard its partial
metrics and let the others continue. Variants execute in process; this harness
does not claim a timeout, crash barrier or host performance measurement.

Example::

    python3 -B -m eval.screen_id_eval --variant eval.v2x.sf_tiledesc \
        --per-kind 2 --frames 8 --size 640 360 --out screen-id.json
"""

import argparse
from dataclasses import fields, is_dataclass
import importlib
from itertools import combinations, combinations_with_replacement
import json
from pathlib import Path
import sys
from time import perf_counter_ns
import traceback
from types import ModuleType

import numpy as np

from visioncore.errors import VqaError
from visioncore.layout import grid_params

from .gui_synth import gen, scenes, splits


SAME_SCREEN_KINDS = (
    "static_clean", "aa_jitter", "caret_blink", "typing", "caret_move",
    "canvas_anim", "window_drag", "scroll_small", "scroll_h", "scroll_smooth",
)
TRANSITION_KINDS = ("scroll_large", "scroll_page")
SCHEMA = "vision-engine.screen-id-eval/v1"
PPM = 1_000_000


class VariantContractError(ValueError):
    """A variant violated the common immutable fingerprint API."""


def canonical_json(value):
    """Canonical JSON text; the CLI adds one terminating newline."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _tagged(value):
    kind = type(value)
    if value is None:
        return ["none"]
    if kind is bool:
        return ["bool", value]
    if kind is int:
        return ["int", value]
    if kind is str:
        return ["str", value]
    if kind is bytes:
        return ["bytes", value.hex()]
    if kind is tuple:
        return ["tuple", [_tagged(item) for item in value]]
    if isinstance(value, tuple) and hasattr(kind, "_fields") and not hasattr(value, "__dict__"):
        return ["namedtuple", kind.__module__ + "." + kind.__qualname__,
                list(kind._fields), [_tagged(item) for item in value]]
    if kind is frozenset:
        items = [_tagged(item) for item in value]
        return ["frozenset", sorted(items, key=canonical_json)]
    if (not isinstance(value, type) and is_dataclass(value)
            and kind.__dataclass_params__.frozen):
        return ["dataclass", kind.__module__ + "." + kind.__qualname__,
                [[field.name, _tagged(getattr(value, field.name))]
                 for field in fields(value)]]
    raise VariantContractError("fingerprint must use a supported deeply immutable representation")


def _fingerprint_size(fingerprint):
    try:
        hash(fingerprint)
    except Exception as exc:
        raise VariantContractError("fingerprint must be hashable") from exc
    if type(fingerprint) is bytes:
        return len(fingerprint), "bytes_payload"
    return len(canonical_json(_tagged(fingerprint)).encode("utf-8")), "tagged_json_utf8"


def _counts():
    return {"pairs": 0, "same": 0, "distance_sum": 0,
            "distance_min": None, "distance_max": None}


def _add(counts, distance, same):
    counts["pairs"] += 1
    counts["same"] += int(same)
    counts["distance_sum"] += distance
    low, high = counts["distance_min"], counts["distance_max"]
    counts["distance_min"] = distance if low is None else min(low, distance)
    counts["distance_max"] = distance if high is None else max(high, distance)


def _summary(counts, expected_same=None):
    pairs, same = counts["pairs"], counts["same"]
    different = pairs - same
    correct = None if expected_same is None else (same if expected_same else different)
    return {
        "pairs": pairs, "correct": correct,
        "value_ppm": None if not pairs or correct is None else correct * PPM // pairs,
        "predicted_same": same, "predicted_different": different,
        "same_screen_rate_ppm": same * PPM // pairs if pairs else None,
        "different_screen_rate_ppm": different * PPM // pairs if pairs else None,
        "distance_min": counts["distance_min"],
        "distance_max": counts["distance_max"],
        "distance_mean_floor": counts["distance_sum"] // pairs if pairs else None,
    }


def _new_state(name, kinds):
    return {
        "name": name, "module": None, "error": None, "first_frames": [],
        "same": _counts(), "different": _counts(), "transition": _counts(),
        "within": {kind: _counts() for kind in kinds},
        "separation": {kind: _counts() for kind in kinds},
        "kind_pairs": {pair: _counts() for pair in combinations_with_replacement(kinds, 2)},
        "sizes": [], "size_encodings": {},
        "diagnostics": {"label": "VM", "setup_wall_time_ns": 0,
                        "fingerprint_wall_time_ns": 0, "comparison_wall_time_ns": 0,
                        "fingerprint_calls": 0, "comparison_calls": 0},
    }


def _fail(state, stage, exc):
    state["error"] = {"stage": stage, "type": type(exc).__name__}
    state["diagnostics"]["failure"] = {
        "stage": stage, "type": type(exc).__name__, "message": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
    # An unsuccessful variant cannot expose partial competitive metrics.
    state["first_frames"].clear()
    state["sizes"].clear()


def _compare(state, left, right):
    started = perf_counter_ns()
    state["diagnostics"]["comparison_calls"] += 1
    try:
        module = state["module"]
        distance = module.fingerprint_distance(left, right)
        if type(distance) is not int or distance < 0:
            raise VariantContractError("fingerprint_distance must return a nonnegative Python int")
        same = module.same_screen(left, right)
        if type(same) is not bool:
            raise VariantContractError("same_screen must return an exact Python bool")
        if same != (distance <= state["threshold"]):
            raise VariantContractError("same_screen must equal distance <= SF_THRESHOLD")
        return distance, same
    finally:
        state["diagnostics"]["comparison_wall_time_ns"] += perf_counter_ns() - started


def _validate_config(variants, per_kind, frames, size, kinds):
    if type(per_kind) is not int or per_kind < 1:
        raise ValueError("per_kind must be a positive Python int")
    dev_seeds = tuple(splits.seeds("DEV"))
    if per_kind > len(dev_seeds):
        raise ValueError("per_kind exceeds the 64 DEV seeds (0..63)")
    seeds = dev_seeds[:per_kind]
    splits.check("DEV", seeds, purpose="evaluate")
    if type(frames) is not int or frames < 1:
        raise ValueError("frames must be a positive Python int")
    if (not isinstance(size, (list, tuple)) or len(size) != 2
            or any(type(value) is not int or value < 1 for value in size)):
        raise ValueError("size must contain positive Python ints (width, height)")
    try:
        grid_params(*size)
    except VqaError as exc:
        raise ValueError("size must satisfy visioncore.layout.grid_params") from exc
    kinds = tuple(scenes.KINDS if kinds is None else kinds)
    if (not kinds or any(type(kind) is not str or kind not in scenes.KINDS for kind in kinds)
            or len(set(kinds)) != len(kinds)):
        raise ValueError("kinds must be a nonempty sequence of distinct known DEV kinds")
    if set(kinds) - set(SAME_SCREEN_KINDS + TRANSITION_KINDS):
        raise ValueError("a selected kind has no screen-identity ground truth")
    if isinstance(variants, (str, ModuleType)):
        variants = [variants]
    variants = list(variants)
    named = []
    for variant in variants:
        if isinstance(variant, str) and variant:
            name = variant
        elif isinstance(variant, ModuleType):
            name = variant.__name__
        else:
            raise ValueError("variants must contain importable module names or module objects")
        named.append((name, variant))
    if not named or len({name for name, _ in named}) != len(named):
        raise ValueError("provide at least one variant and no duplicate module names")
    return sorted(named), tuple(sorted(kinds)), seeds, tuple(size)


def _result_row(state, kinds, per_kind):
    if state["error"] is not None:
        return {"status": "FAILED", "error": state["error"]}
    sizes = state["sizes"]
    return {
        "status": "OK", "threshold": state["threshold"],
        "same_screen_recall": _summary(state["same"], True),
        "different_screen_separation": _summary(state["different"], False),
        "transitions": {
            "excluded_from_recall": True,
            "all": _summary(state["transition"]),
            **{kind: _summary(state["within"][kind])
               for kind in kinds if kind in TRANSITION_KINDS},
        },
        "per_kind": {
            kind: {
                "scene_count": per_kind,
                "included_in_recall": kind in SAME_SCREEN_KINDS,
                "same_screen_recall": (_summary(state["within"][kind], True)
                                       if kind in SAME_SCREEN_KINDS else None),
                "transition": (_summary(state["within"][kind])
                               if kind in TRANSITION_KINDS else None),
                "different_screen_separation": _summary(state["separation"][kind], False),
            } for kind in kinds
        },
        "kind_pairs": [
            {"kind_a": pair[0], "kind_b": pair[1],
             "different_screen_separation": _summary(counts, False)}
            for pair, counts in state["kind_pairs"].items()
        ],
        "fingerprint_size_bytes": {
            "count": len(sizes), "min": min(sizes) if sizes else None,
            "max": max(sizes) if sizes else None, "total": sum(sizes),
            "mean_floor": sum(sizes) // len(sizes) if sizes else None,
            "encoding_counts": state["size_encodings"],
            "definition": "bytes_payload=len(bytes); other supported immutable values use tagged_json_utf8; excludes Python heap overhead",
        },
    }


def evaluate(variants, *, per_kind=2, frames=8, size=(640, 360), kinds=None, diagnostics=None):
    """Return deterministic DEV metrics; optionally fill separate VM diagnostics.

    Variant failures are results, not uncaught exceptions. Invalid run arguments
    and synthesizer failures abort the run rather than imply valid evidence.
    No GATE/TEST selector or pixel-output option exists.
    """
    total_started = perf_counter_ns()
    named, kinds, seeds, size = _validate_config(variants, per_kind, frames, size, kinds)
    generation_started = perf_counter_ns()
    catalogue = [scenes.build(kind, k, w=size[0], h=size[1], n=frames, split="DEV")
                 for kind in kinds for k in seeds]
    vm = {"schema": SCHEMA + "/diagnostics", "label": "VM", "unit": "ns",
          "scene_build_wall_time_ns": perf_counter_ns() - generation_started,
          "render_and_readonly_copy_wall_time_ns": 0, "frames_rendered": 0,
          "variants": {}}
    states = [_new_state(name, kinds) for name, _ in named]
    for state, (_, variant) in zip(states, named):
        started = perf_counter_ns()
        try:
            module = importlib.import_module(variant) if isinstance(variant, str) else variant
            for attr in ("screen_fingerprint", "fingerprint_distance", "same_screen"):
                if not callable(getattr(module, attr, None)):
                    raise VariantContractError(attr + " must be callable")
            threshold = getattr(module, "SF_THRESHOLD", None)
            if type(threshold) is not int:
                raise VariantContractError("SF_THRESHOLD must be an exact Python int")
            state["module"], state["threshold"] = module, threshold
        except (Exception, SystemExit) as exc:
            _fail(state, "setup", exc)
        finally:
            state["diagnostics"]["setup_wall_time_ns"] += perf_counter_ns() - started

    for scene in catalogue:
        active = [state for state in states if state["error"] is None]
        if not active:
            break
        fingerprints = {state["name"]: [] for state in active}
        rendered = iter(gen.render(scene))
        for frame_index in range(frames):
            started = perf_counter_ns()
            index, raw_frame = next(rendered)
            if (index != frame_index or not isinstance(raw_frame, np.ndarray)
                    or raw_frame.dtype != np.uint8 or raw_frame.shape != (size[1], size[0], 3)):
                raise ValueError("render must yield ordered uint8 RGB frames of the requested size")
            # Immutable storage, including through .base, defeats accidental
            # write-flag re-enabling without retaining a second scene's frames.
            readonly_frame = np.frombuffer(raw_frame.tobytes(order="C"), dtype=np.uint8).reshape(raw_frame.shape)
            vm["render_and_readonly_copy_wall_time_ns"] += perf_counter_ns() - started
            vm["frames_rendered"] += 1
            for state in active:
                if state["error"] is not None:
                    continue
                started = perf_counter_ns()
                state["diagnostics"]["fingerprint_calls"] += 1
                variant_frame = readonly_frame.view()
                try:
                    fingerprint = state["module"].screen_fingerprint(variant_frame)
                    if (variant_frame.shape != readonly_frame.shape
                            or variant_frame.strides != readonly_frame.strides
                            or variant_frame.dtype != readonly_frame.dtype
                            or variant_frame.flags.writeable):
                        raise VariantContractError("screen_fingerprint must not mutate input array metadata")
                    nbytes, encoding = _fingerprint_size(fingerprint)
                    fingerprints[state["name"]].append(fingerprint)
                    state["sizes"].append(nbytes)
                    encodings = state["size_encodings"]
                    encodings[encoding] = encodings.get(encoding, 0) + 1
                except (Exception, SystemExit) as exc:
                    _fail(state, "fingerprint:" + scene["scene_id"] + ":" + str(frame_index), exc)
                    fingerprints[state["name"]].clear()
                finally:
                    state["diagnostics"]["fingerprint_wall_time_ns"] += perf_counter_ns() - started
                    del variant_frame
            del readonly_frame, raw_frame
        rendered.close()
        for state in active:
            if state["error"] is not None:
                continue
            fps = fingerprints[state["name"]]
            try:
                kind = scene["kind"]
                for left, right in combinations(fps, 2):
                    distance, same = _compare(state, left, right)
                    _add(state["within"][kind], distance, same)
                    category = "same" if kind in SAME_SCREEN_KINDS else "transition"
                    _add(state[category], distance, same)
                state["first_frames"].append((kind, scene["k"], fps[0]))
            except (Exception, SystemExit) as exc:
                _fail(state, "within_scene:" + scene["scene_id"], exc)

    for state in states:
        if state["error"] is not None:
            continue
        try:
            for left, right in combinations(state["first_frames"], 2):
                distance, same = _compare(state, left[2], right[2])
                _add(state["different"], distance, same)
                pair = tuple(sorted((left[0], right[0])))
                _add(state["kind_pairs"][pair], distance, same)
                for kind in sorted(set(pair)):
                    _add(state["separation"][kind], distance, same)
        except (Exception, SystemExit) as exc:
            _fail(state, "between_scenes", exc)

    result = {
        "schema": SCHEMA, "split": "DEV",
        "config": {"variants": [name for name, _ in named], "per_kind": per_kind,
                   "frames_per_scene": frames, "size": list(size),
                   "kinds": list(kinds), "seeds": list(seeds)},
        "scenes": [{"kind": scene["kind"], "k": scene["k"], "scene_id": scene["scene_id"]}
                   for scene in catalogue],
        "ground_truth": {
            "same_screen_kinds": sorted(SAME_SCREEN_KINDS),
            "transition_kinds": sorted(TRANSITION_KINDS),
            "recall_pairs": "all unordered within-scene frame pairs of same_screen_kinds",
            "separation_pairs": "all unordered first-frame pairs of distinct scenes (kind or k differs)",
            "per_kind_separation": "each pair counted once per distinct endpoint kind; rows overlap",
            "rates": "integer ppm, floor; null when undefined",
        },
        "variants": {state["name"]: _result_row(state, kinds, per_kind) for state in states},
        "timing": {"label": "VM", "unit": "ns", "clock": "perf_counter_ns",
                   "location": "CLI stderr or Python diagnostics mapping",
                   "included_in_canonical_metrics": False},
    }
    for state in states:
        state["diagnostics"]["status"] = "FAILED" if state["error"] is not None else "OK"
        vm["variants"][state["name"]] = state["diagnostics"]
    vm["total_wall_time_ns"] = perf_counter_ns() - total_started
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(vm)
    return result


run = evaluate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", action="append", required=True, help="sf_ module name; repeatable")
    parser.add_argument("--per-kind", type=int, default=2)
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--size", nargs=2, type=int, default=(640, 360), metavar=("W", "H"))
    parser.add_argument("--kind", action="append", choices=scenes.KINDS, help="DEV kind subset; repeatable")
    parser.add_argument("--out", type=Path, help="canonical metrics JSON; defaults to stdout")
    args = parser.parse_args(argv)
    vm = {}
    try:
        result = evaluate(args.variant, per_kind=args.per_kind, frames=args.frames,
                          size=tuple(args.size), kinds=args.kind, diagnostics=vm)
        payload = canonical_json(result) + "\n"
        if args.out is None:
            sys.stdout.write(payload)
        else:
            args.out.write_text(payload, encoding="utf-8")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    sys.stderr.write(canonical_json(vm) + "\n")
    return 1 if any(row["status"] == "FAILED" for row in result["variants"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
