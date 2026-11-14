#!/usr/bin/env python3
"""Independent validator for resumable V2EVAL2 partial outputs."""

import argparse
import json
from pathlib import Path
import sys

# Ensure repository root is on sys.path if run directly as script
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.gui_synth.score import aggregate
from eval.v2eval.exploration import leaderboard
from eval.v2eval.run import add_honesty


def _json_canonical(obj):
    """Normalize object through JSON dump/load cycle so tuples become lists."""
    return json.loads(json.dumps(obj))


def check_partial_dir(target_dir: Path):
    reasons = []
    valid = True

    rows_file = target_dir / "rows.jsonl"
    lb_file = target_dir / "LEADERBOARD.partial.json"

    if not lb_file.is_file():
        return False, {"valid": False, "reasons": ["LEADERBOARD.partial.json missing"]}

    try:
        lb_data = json.loads(lb_file.read_text())
    except Exception as e:
        return False, {"valid": False, "reasons": [f"LEADERBOARD.partial.json invalid JSON: {e}"]}

    # Validate LEADERBOARD.partial.json top-level keys
    for k in ("complete_variants", "pending_variants", "rows"):
        if k not in lb_data:
            valid = False
            reasons.append(f"LEADERBOARD.partial.json missing key: {k}")

    complete_variants = lb_data.get("complete_variants", [])
    pending_variants = lb_data.get("pending_variants", [])

    # Check pending variants in leaderboard rows
    lb_variant_names = [r.get("variant") for r in lb_data.get("rows", []) if isinstance(r, dict)]
    for p_var in pending_variants:
        if p_var in lb_variant_names:
            valid = False
            reasons.append(f"Pending variant '{p_var}' appears in partial leaderboard rows")

    # Read rows.jsonl
    parsed_rows = []
    if rows_file.is_file():
        lines = rows_file.read_text().splitlines()
        for i, line in enumerate(lines):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                row_obj = json.loads(line_str)
                parsed_rows.append(row_obj)
            except json.JSONDecodeError:
                # The last line may be truncated
                if i == len(lines) - 1:
                    pass
                else:
                    valid = False
                    reasons.append(f"rows.jsonl line {i + 1} is not valid JSON and is not the last line")

    # Check duplicate (variant, scene_id) pairs
    seen_pairs = set()
    harness_hashes = set()
    variant_scenes = {}  # var_name -> set(scene_id)
    variant_records = {}  # var_name -> list of row_objs

    for r in parsed_rows:
        if not isinstance(r, dict):
            valid = False
            reasons.append("Row in rows.jsonl is not a JSON object")
            continue

        var_name = r.get("variant")
        scene_id = r.get("scene_id")

        if not scene_id or not isinstance(scene_id, str) or not scene_id.startswith("DEV:"):
            valid = False
            reasons.append(f"Row scene_id '{scene_id}' does not start with 'DEV:'")

        pair = (var_name, scene_id)
        if pair in seen_pairs:
            valid = False
            reasons.append(f"Duplicate (variant, scene_id) pair: {pair}")
        seen_pairs.add(pair)

        if "harness_sha256" in r and r["harness_sha256"] is not None:
            harness_hashes.add(r["harness_sha256"])

        variant_scenes.setdefault(var_name, set()).add(scene_id)
        variant_records.setdefault(var_name, []).append(r)

    # Check one harness_sha256 across all rows
    if len(harness_hashes) > 1:
        valid = False
        reasons.append(f"Multiple harness_sha256 found in rows.jsonl: {sorted(harness_hashes)}")

    harness_sha256 = next(iter(harness_hashes), None)
    if harness_sha256 is not None and "harness_sha256" in lb_data:
        if lb_data["harness_sha256"] != harness_sha256:
            valid = False
            reasons.append(
                f"LEADERBOARD.partial.json harness_sha256 ({lb_data.get('harness_sha256')}) "
                f"differs from rows.jsonl harness_sha256 ({harness_sha256})"
            )

    # All scene_id in LEADERBOARD.partial.json scene_ids must start with DEV:
    lb_scene_ids = lb_data.get("scene_ids", [])
    for sid in lb_scene_ids:
        if not isinstance(sid, str) or not sid.startswith("DEV:"):
            valid = False
            reasons.append(f"LEADERBOARD.partial.json scene_id '{sid}' does not start with 'DEV:'")

    # Baseline scenes set
    baseline_scenes = variant_scenes.get("baseline", set())
    if not baseline_scenes and lb_scene_ids:
        baseline_scenes = set(lb_scene_ids)

    # Every complete variant has exactly one row per scene of the baseline
    for c_var in complete_variants:
        c_scenes = variant_scenes.get(c_var, set())
        if c_scenes != baseline_scenes:
            valid = False
            reasons.append(
                f"Complete variant '{c_var}' scenes {sorted(c_scenes)} do not match baseline scenes {sorted(baseline_scenes)}"
            )

    # Reconstruct data structure to pass to leaderboard() function and compare rows
    reconstructed_variants = {}

    all_vars_in_rows = sorted(variant_records.keys())
    for var_name in all_vars_in_rows:
        rows = variant_records[var_name]
        if not rows:
            continue

        status = "OK"
        error = None
        module_sha256 = None
        false_relabels = 0
        static_jitter_tile_frames = 0
        python_opcodes = 0
        calls = 0
        has_opcodes = False
        has_calls = False

        metric_rows = []

        for r in rows:
            if r.get("status") == "FAILED":
                status = "FAILED"
                error = r.get("error")
            if r.get("module_sha256"):
                module_sha256 = r.get("module_sha256")
            false_relabels += r.get("false_relabels") or 0
            static_jitter_tile_frames += r.get("static_jitter_tile_frames") or 0

            if r.get("python_opcodes") is not None:
                python_opcodes += r.get("python_opcodes")
                has_opcodes = True
            if r.get("calls") is not None:
                calls += r.get("calls")
                has_calls = True

            if r.get("row") is not None:
                metric_rows.append(r["row"])

        metrics = None
        if status == "OK" and metric_rows:
            try:
                aggregated = aggregate(metric_rows)
                metrics = add_honesty(aggregated)
            except Exception as e:
                valid = False
                reasons.append(f"Error aggregating metrics for variant '{var_name}': {e}")

        reconstructed_variants[var_name] = {
            "status": status,
            "error": error,
            "false_relabels": false_relabels,
            "static_jitter_tile_frames": static_jitter_tile_frames,
            "python_opcodes": python_opcodes if (has_opcodes and var_name != "baseline" and status == "OK") else None,
            "calls": calls if (has_calls and var_name != "baseline" and status == "OK") else None,
            "module_sha256": module_sha256,
            "metrics": metrics,
        }

    reconstructed_data = {
        "split": "DEV",
        "scene_ids": sorted(list(baseline_scenes)) if baseline_scenes else lb_data.get("scene_ids", []),
        "frame_budget_ms": lb_data.get("frame_budget_ms", 1000),
        "cost_scope": lb_data.get(
            "cost_scope",
            "executed Python bytecodes in frame API calls and Python dependencies; NumPy/C internals excluded; baseline uninstrumented",
        ),
        "prereg_sha256": lb_data.get("prereg_sha256"),
        "harness_sha256": harness_sha256 or lb_data.get("harness_sha256"),
        "variants": {v: reconstructed_variants[v] for v in complete_variants if v in reconstructed_variants},
    }

    if complete_variants:
        try:
            recomputed_lb = leaderboard(reconstructed_data)
            recomputed_rows = _json_canonical(recomputed_lb.get("rows", []))
            lb_rows = _json_canonical(lb_data.get("rows", []))

            if recomputed_rows != lb_rows:
                valid = False
                reasons.append(
                    f"Leaderboard row values do not match recomputation from rows.jsonl.\n"
                    f"Expected: {json.dumps(recomputed_rows, sort_keys=True)}\n"
                    f"Actual: {json.dumps(lb_rows, sort_keys=True)}"
                )
        except Exception as e:
            valid = False
            reasons.append(f"Failed to recompute leaderboard: {e}")

    report = {"valid": valid, "reasons": reasons}
    return valid, report


def main():
    parser = argparse.ArgumentParser(description="Validator for resumable V2EVAL2 partial outputs.")
    parser.add_argument("--dir", required=True, type=Path, help="Directory containing partial outputs")
    args = parser.parse_args()

    valid, report = check_partial_dir(args.dir)
    print(json.dumps(report, indent=2))
    sys.exit(0 if valid else 1)


if __name__ == "__main__":
    main()
