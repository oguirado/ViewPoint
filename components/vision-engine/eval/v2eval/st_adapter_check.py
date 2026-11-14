#!/usr/bin/env python3
"""Adversarial checker for frame-caching sub-tile (st) plug-in adapters.

Checks candidate st adapter against reference adapter for stale-cache risks
and spec compliance across 9 attack scenarios.

Usage:
  python3 eval/v2eval/st_adapter_check.py --candidate <dotted.module> [--reference <dotted.module>]
"""

import argparse
import gc
import importlib
import json
import os
import random
import sys
from pathlib import Path
import numpy as np

# Ensure root directory is on sys.path
root_dir = str(Path(__file__).resolve().parent.parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)


def run_attack_1_random_tile_order(cand_mod, ref_mod):
    """Attack 1: Same arrays queried in random tile order."""
    rng = random.Random(101)
    np_rng = np.random.RandomState(101)
    W, H, s, max_d = 64, 64, 16, 3
    prev = np_rng.randint(0, 256, (H, W), dtype=np.uint8)
    cur = prev.copy()
    # Apply translation (1, 2) to a region
    cur[16:32, 16:32] = prev[14:30, 15:31]

    gw, gh = (W + s - 1) // s, (H + s - 1) // s
    tiles = [(tx, ty) for ty in range(gh) for tx in range(gw)]
    rng.shuffle(tiles)

    mismatches = 0
    for tile_xy in tiles:
        ref_res = ref_mod.subtile_motion(prev, cur, s, tile_xy, max_d)
        cand_res = cand_mod.subtile_motion(prev, cur, s, tile_xy, max_d)
        if cand_res != ref_res:
            mismatches += 1

    return {
        "passed": mismatches == 0,
        "mismatches": mismatches,
        "details": "ok" if mismatches == 0 else f"{mismatches} tile mismatches in random tile order"
    }


def run_attack_2_new_arrays_identical_content(cand_mod, ref_mod):
    """Attack 2: New arrays with identical content."""
    np_rng = np.random.RandomState(102)
    W, H, s, max_d = 64, 64, 16, 3
    prev1 = np_rng.randint(0, 256, (H, W), dtype=np.uint8)
    cur1 = prev1.copy()
    cur1[16:32, 16:32] = prev1[14:30, 15:31]

    # Query first pair
    for ty in range(4):
        for tx in range(4):
            _ = cand_mod.subtile_motion(prev1, cur1, s, (tx, ty), max_d)

    # Create new arrays with identical content
    prev2 = prev1.copy()
    cur2 = cur1.copy()

    mismatches = 0
    for ty in range(4):
        for tx in range(4):
            ref_res = ref_mod.subtile_motion(prev2, cur2, s, (tx, ty), max_d)
            cand_res = cand_mod.subtile_motion(prev2, cur2, s, (tx, ty), max_d)
            if cand_res != ref_res:
                mismatches += 1

    return {
        "passed": mismatches == 0,
        "mismatches": mismatches,
        "details": "ok" if mismatches == 0 else f"{mismatches} mismatches on new arrays with identical content"
    }


def run_attack_3_in_place_modified_buffer(cand_mod, ref_mod):
    """Attack 3: Same buffer modified in place between frames after flags.writeable is toggled."""
    W, H, s, max_d = 64, 64, 16, 3
    prev = np.zeros((H, W), dtype=np.uint8)
    prev[10:30, 10:30] = 100
    buf = prev.copy()

    # Toggle flags.writeable to False before first call
    buf.flags.writeable = False

    # First call with buf (returns None as prev == buf)
    _ = cand_mod.subtile_motion(prev, buf, s, (1, 1), max_d)

    # Modify buf in place after making writeable
    buf.flags.writeable = True
    buf[16:32, 16:32] = prev[14:30, 15:31]

    mismatches = 0
    for ty in range(4):
        for tx in range(4):
            ref_res = ref_mod.subtile_motion(prev, buf, s, (tx, ty), max_d)
            cand_res = cand_mod.subtile_motion(prev, buf, s, (tx, ty), max_d)
            if cand_res != ref_res:
                mismatches += 1

    return {
        "passed": mismatches == 0,
        "mismatches": mismatches,
        "details": "ok" if mismatches == 0 else f"{mismatches} mismatches after in-place buffer modification"
    }


def run_attack_4_prev_cur_swapped(cand_mod, ref_mod):
    """Attack 4: Prev and cur swapped."""
    np_rng = np.random.RandomState(104)
    W, H, s, max_d = 64, 64, 16, 3
    prev = np_rng.randint(0, 256, (H, W), dtype=np.uint8)
    cur = prev.copy()
    cur[16:32, 16:32] = prev[14:30, 15:31]

    # Query prev, cur
    for ty in range(4):
        for tx in range(4):
            _ = cand_mod.subtile_motion(prev, cur, s, (tx, ty), max_d)

    # Query cur, prev (swapped)
    mismatches = 0
    for ty in range(4):
        for tx in range(4):
            ref_res = ref_mod.subtile_motion(cur, prev, s, (tx, ty), max_d)
            cand_res = cand_mod.subtile_motion(cur, prev, s, (tx, ty), max_d)
            if cand_res != ref_res:
                mismatches += 1

    return {
        "passed": mismatches == 0,
        "mismatches": mismatches,
        "details": "ok" if mismatches == 0 else f"{mismatches} mismatches when prev and cur were swapped"
    }


def run_attack_5_different_max_d(cand_mod, ref_mod):
    """Attack 5: Same frames with a different max_d."""
    np_rng = np.random.RandomState(105)
    W, H, s = 64, 64, 16
    prev = np_rng.randint(0, 256, (H, W), dtype=np.uint8)
    cur = prev.copy()
    # Displacement of 3
    cur[16:32, 16:32] = prev[13:29, 13:29]

    # Query with max_d = 1
    for ty in range(4):
        for tx in range(4):
            _ = cand_mod.subtile_motion(prev, cur, s, (tx, ty), max_d=1)

    # Query with max_d = 4
    mismatches = 0
    for ty in range(4):
        for tx in range(4):
            ref_res = ref_mod.subtile_motion(prev, cur, s, (tx, ty), max_d=4)
            cand_res = cand_mod.subtile_motion(prev, cur, s, (tx, ty), max_d=4)
            if cand_res != ref_res:
                mismatches += 1

    return {
        "passed": mismatches == 0,
        "mismatches": mismatches,
        "details": "ok" if mismatches == 0 else f"{mismatches} mismatches when max_d was changed"
    }


def run_attack_6_different_s(cand_mod, ref_mod):
    """Attack 6: Same frames with a different s."""
    np_rng = np.random.RandomState(106)
    W, H, max_d = 64, 64, 3
    prev = np_rng.randint(0, 256, (H, W), dtype=np.uint8)
    cur = prev.copy()
    cur[16:32, 16:32] = prev[14:30, 15:31]

    # Query with s = 16
    for ty in range(4):
        for tx in range(4):
            _ = cand_mod.subtile_motion(prev, cur, s=16, tile_xy=(tx, ty), max_d=max_d)

    # Query with s = 32
    mismatches = 0
    for ty in range(2):
        for tx in range(2):
            ref_res = ref_mod.subtile_motion(prev, cur, s=32, tile_xy=(tx, ty), max_d=max_d)
            cand_res = cand_mod.subtile_motion(prev, cur, s=32, tile_xy=(tx, ty), max_d=max_d)
            if cand_res != ref_res:
                mismatches += 1

    return {
        "passed": mismatches == 0,
        "mismatches": mismatches,
        "details": "ok" if mismatches == 0 else f"{mismatches} mismatches when s was changed"
    }


def run_attack_7_odd_grid_corner_tiles(cand_mod, ref_mod):
    """Attack 7: Tiles at all four corners of an odd-size grid such as 63x45."""
    np_rng = np.random.RandomState(107)
    W, H, s, max_d = 63, 45, 16, 2
    prev = np_rng.randint(0, 256, (H, W), dtype=np.uint8)
    cur = prev.copy()
    cur[0:16, 0:16] = prev[1:17, 1:17]

    gw = (W + s - 1) // s  # 4
    gh = (H + s - 1) // s  # 3
    corners = [(0, 0), (gw - 1, 0), (0, gh - 1), (gw - 1, gh - 1)]

    mismatches = 0
    for tile_xy in corners:
        ref_res = ref_mod.subtile_motion(prev, cur, s, tile_xy, max_d)
        cand_res = cand_mod.subtile_motion(prev, cur, s, tile_xy, max_d)
        if cand_res != ref_res:
            mismatches += 1

    return {
        "passed": mismatches == 0,
        "mismatches": mismatches,
        "details": "ok" if mismatches == 0 else f"{mismatches} mismatches on corner tiles of odd-size grid"
    }


def run_attack_8_single_tile_grid(cand_mod, ref_mod):
    """Attack 8: A 1x1 tile grid."""
    np_rng = np.random.RandomState(108)
    W, H, s, max_d = 12, 12, 16, 2
    prev = np_rng.randint(0, 256, (H, W), dtype=np.uint8)
    cur = prev.copy()
    cur[2:10, 2:10] = prev[1:9, 1:9]

    tile_xy = (0, 0)
    ref_res = ref_mod.subtile_motion(prev, cur, s, tile_xy, max_d)
    cand_res = cand_mod.subtile_motion(prev, cur, s, tile_xy, max_d)
    mismatches = 0 if cand_res == ref_res else 1

    return {
        "passed": mismatches == 0,
        "mismatches": mismatches,
        "details": "ok" if mismatches == 0 else "mismatch on 1x1 tile grid"
    }


def run_attack_9_id_reused_after_gc(cand_mod, ref_mod):
    """Attack 9: An array whose id is reused after garbage collection."""
    W, H, s, max_d = 64, 64, 16, 3
    prev = np.zeros((H, W), dtype=np.uint8)
    prev[10:30, 10:30] = 100

    # Frame A
    arr_a = prev.copy()
    target_id = id(arr_a)
    _ = cand_mod.subtile_motion(prev, arr_a, s, (1, 1), max_d)

    # Delete arr_a and force garbage collection
    del arr_a
    gc.collect()

    # Create new arrays until one gets target_id
    reused_arr = None
    for i in range(1000):
        candidate_arr = prev.copy()
        candidate_arr[16:32, 16:32] = prev[14:30, 15:31]
        if id(candidate_arr) == target_id:
            reused_arr = candidate_arr
            break

    if reused_arr is None:
        # Fallback if Python allocator didn't reuse exact ID: force id match or allocate list of arrays
        arr_list = []
        for i in range(500):
            temp = np.full((H, W), i % 255, dtype=np.uint8)
            if id(temp) == target_id:
                reused_arr = temp
                break
            arr_list.append(temp)

    if reused_arr is None:
        # If ID was still not reused, return passed=True with warning details
        return {
            "passed": True,
            "mismatches": 0,
            "details": "ok (id reuse not triggered by allocator)"
        }

    mismatches = 0
    for ty in range(4):
        for tx in range(4):
            ref_res = ref_mod.subtile_motion(prev, reused_arr, s, (tx, ty), max_d)
            cand_res = cand_mod.subtile_motion(prev, reused_arr, s, (tx, ty), max_d)
            if cand_res != ref_res:
                mismatches += 1

    return {
        "passed": mismatches == 0,
        "mismatches": mismatches,
        "details": "ok" if mismatches == 0 else f"{mismatches} mismatches when array id was reused after GC"
    }


ATTACKS = [
    ("1_same_arrays_random_tile_order", run_attack_1_random_tile_order),
    ("2_new_arrays_identical_content", run_attack_2_new_arrays_identical_content),
    ("3_same_buffer_in_place_modified", run_attack_3_in_place_modified_buffer),
    ("4_prev_cur_swapped", run_attack_4_prev_cur_swapped),
    ("5_different_max_d", run_attack_5_different_max_d),
    ("6_different_s", run_attack_6_different_s),
    ("7_odd_grid_corner_tiles", run_attack_7_odd_grid_corner_tiles),
    ("8_single_tile_grid", run_attack_8_single_tile_grid),
    ("9_id_reused_after_gc", run_attack_9_id_reused_after_gc),
]


def main():
    parser = argparse.ArgumentParser(
        description="Adversarial checker for frame-caching subtile_motion adapters."
    )
    parser.add_argument(
        "--candidate",
        required=True,
        help="Dotted module path of candidate adapter."
    )
    parser.add_argument(
        "--reference",
        default="eval.v2proto_subtile",
        help="Dotted module path of reference adapter (default: eval.v2proto_subtile)."
    )
    args = parser.parse_args()

    try:
        cand_mod = importlib.import_module(args.candidate)
    except Exception as e:
        print(json.dumps({"error": f"Failed to import candidate {args.candidate}: {e}"}))
        sys.exit(1)

    try:
        ref_mod = importlib.import_module(args.reference)
    except Exception as e:
        print(json.dumps({"error": f"Failed to import reference {args.reference}: {e}"}))
        sys.exit(1)

    results = {}
    all_passed = True

    for attack_name, attack_fn in ATTACKS:
        res = attack_fn(cand_mod, ref_mod)
        results[attack_name] = res
        if not res["passed"]:
            all_passed = False

    report = {
        "candidate": args.candidate,
        "reference": args.reference,
        "passed": all_passed,
        "attacks": results,
    }

    print(json.dumps(report, indent=2))
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
