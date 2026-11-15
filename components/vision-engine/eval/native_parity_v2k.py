"""Host instrument that judges V2EVAL2 st or gm plug-in module parity against a Python reference."""

import argparse
import importlib
import json
import sys
import numpy as np
from visioncore.layout import grid_params


def generate_frame_pairs(W, H, rng):
    """Generate 5 content scenario frame pairs for a given resolution (W, H)."""
    pairs = []

    # 1. Identical
    p1 = rng.randint(0, 256, size=(H, W)).astype(np.uint8)
    c1 = p1.copy()
    pairs.append(("identical", p1, c1))

    # 2. Globally shifted (dx=3, dy=-2)
    p2 = rng.randint(20, 230, size=(H, W)).astype(np.uint8)
    c2 = np.roll(p2, shift=(-2, 3), axis=(0, 1))
    pairs.append(("globally_shifted", p2, c2))

    # 3. Locally shifted patch
    p3 = rng.randint(20, 230, size=(H, W)).astype(np.uint8)
    c3 = p3.copy()
    ph, pw = max(8, min(16, H // 2)), max(8, min(16, W // 2))
    py0, px0 = max(0, H // 4), max(0, W // 4)
    if py0 + ph + 2 <= H and px0 + pw + 2 <= W:
        c3[py0:py0 + ph, px0:px0 + pw] = p3[py0 + 2:py0 + ph + 2, px0 + 2:px0 + pw + 2]
    pairs.append(("locally_shifted_patch", p3, c3))

    # 4. Random
    p4 = rng.randint(0, 256, size=(H, W)).astype(np.uint8)
    c4 = rng.randint(0, 256, size=(H, W)).astype(np.uint8)
    pairs.append(("random", p4, c4))

    # 5. Low-texture
    p5 = (128 + rng.randint(-2, 3, size=(H, W))).astype(np.uint8)
    c5 = p5.copy()
    c5[H // 2, W // 2] = np.uint8((int(c5[H // 2, W // 2]) + 10) % 256)
    pairs.append(("low_texture", p5, c5))

    return pairs


def _equal_vectors(v1, v2):
    if v1 is None and v2 is None:
        return True
    if v1 is None or v2 is None:
        return False
    if not (isinstance(v1, (tuple, list)) and isinstance(v2, (tuple, list))):
        return False
    if len(v1) != len(v2):
        return False
    return tuple(v1) == tuple(v2)


def check(candidate_module_name, kind, reference_module_name, seed=42, resolutions=None):
    """Check parity of candidate module against reference module.

    Args:
        candidate_module_name (str): Importable dotted name of candidate module.
        kind (str): "st" or "gm".
        reference_module_name (str): Importable dotted name of reference module.
        seed (int): Random seed for frame pair generation.
        resolutions (list, optional): List of (W, H) tuple resolutions. Defaults to [(64, 48), (63, 45), (320, 180), (1280, 720)].

    Returns:
        dict: Parity check JSON report.
    """
    if kind not in ("st", "gm"):
        raise ValueError("kind must be 'st' or 'gm'")

    cand_mod = importlib.import_module(candidate_module_name)
    ref_mod = importlib.import_module(reference_module_name)

    rng = np.random.RandomState(seed)
    if resolutions is None:
        resolutions = [(64, 48), (63, 45), (320, 180), (1280, 720)]

    mismatches = []
    tests_run = 0

    for W, H in resolutions:
        s, gw, gh = grid_params(W, H)
        pairs = generate_frame_pairs(W, H, rng)

        for scenario_name, prev_orig, cur_orig in pairs:
            # Prepare phases
            # Phase A: initial frame pair objects
            p_a, c_a = prev_orig.copy(), cur_orig.copy()

            # Phase B: new frame pair objects with identical content
            p_b, c_b = prev_orig.copy(), cur_orig.copy()

            # Phase C: in-place-modified copy reusing object identity of p_b / c_b
            p_c, c_c = p_b, c_b
            p_c.flags.writeable = True
            c_c.flags.writeable = True
            bh, bw = max(8, min(16, H // 2)), max(8, min(16, W // 2))
            by0, bx0 = max(0, H // 4), max(0, W // 4)
            p_c_block = (p_c[by0:by0 + bh, bx0:bx0 + bw].astype(np.int32) + 100) % 256
            p_c[by0:by0 + bh, bx0:bx0 + bw] = p_c_block.astype(np.uint8)
            c_c[by0:by0 + bh, bx0:bx0 + bw] = np.roll(p_c[by0:by0 + bh, bx0:bx0 + bw], shift=(2, 2), axis=(0, 1))

            phases = [
                ("phase_A", p_a, c_a),
                ("phase_B", p_b, c_b),
                ("phase_C", p_c, c_c),
            ]

            for phase_name, prev_frame, cur_frame in phases:
                prev_frame.flags.writeable = False
                cur_frame.flags.writeable = False

                if kind == "st":
                    max_d = s - 1
                    for ty in range(gh):
                        for tx in range(gw):
                            tests_run += 1
                            tile_xy = (tx, ty)
                            ref_res = ref_mod.subtile_motion(prev_frame, cur_frame, s, tile_xy, max_d)
                            try:
                                cand_res = cand_mod.subtile_motion(prev_frame, cur_frame, s, tile_xy, max_d)
                            except Exception as exc:
                                mismatches.append({
                                    "res": (W, H),
                                    "scenario": scenario_name,
                                    "phase": phase_name,
                                    "tile": tile_xy,
                                    "error": f"Candidate raised exception: {exc}"
                                })
                                continue

                            if not _equal_vectors(ref_res, cand_res):
                                mismatches.append({
                                    "res": (W, H),
                                    "scenario": scenario_name,
                                    "phase": phase_name,
                                    "tile": tile_xy,
                                    "ref": ref_res,
                                    "cand": cand_res
                                })

                elif kind == "gm":
                    max_d = max(W, H) - 1
                    tests_run += 1
                    ref_vec = ref_mod.estimate_global_motion(prev_frame, cur_frame, max_d)
                    try:
                        cand_vec = cand_mod.estimate_global_motion(prev_frame, cur_frame, max_d)
                    except Exception as exc:
                        mismatches.append({
                            "res": (W, H),
                            "scenario": scenario_name,
                            "phase": phase_name,
                            "error": f"Candidate estimate_global_motion raised: {exc}"
                        })
                        continue

                    if not _equal_vectors(ref_vec, cand_vec):
                        mismatches.append({
                            "res": (W, H),
                            "scenario": scenario_name,
                            "phase": phase_name,
                            "type": "estimate_global_motion",
                            "ref": ref_vec,
                            "cand": cand_vec
                        })

                    # Test consensus_tiles
                    # Determine shifts to test in consensus_tiles
                    test_shifts = []
                    if ref_vec is not None:
                        test_shifts.append(ref_vec[:2])
                    if cand_vec is not None and cand_vec[:2] not in test_shifts:
                        test_shifts.append(cand_vec[:2])
                    if (0, 0) not in test_shifts:
                        test_shifts.append((0, 0))

                    for dx, dy in test_shifts:
                        tests_run += 1
                        ref_mask = ref_mod.consensus_tiles(prev_frame, cur_frame, s, dx, dy)
                        try:
                            cand_mask = cand_mod.consensus_tiles(prev_frame, cur_frame, s, dx, dy)
                        except Exception as exc:
                            mismatches.append({
                                "res": (W, H),
                                "scenario": scenario_name,
                                "phase": phase_name,
                                "shift": (dx, dy),
                                "error": f"Candidate consensus_tiles raised: {exc}"
                            })
                            continue

                        ref_m = np.asarray(ref_mask)
                        cand_m = np.asarray(cand_mask)

                        if ref_m.dtype != cand_m.dtype:
                            mismatches.append({
                                "res": (W, H),
                                "scenario": scenario_name,
                                "phase": phase_name,
                                "shift": (dx, dy),
                                "type": "consensus_tiles_dtype",
                                "ref_dtype": str(ref_m.dtype),
                                "cand_dtype": str(cand_m.dtype)
                            })
                        elif ref_m.shape != cand_m.shape:
                            mismatches.append({
                                "res": (W, H),
                                "scenario": scenario_name,
                                "phase": phase_name,
                                "shift": (dx, dy),
                                "type": "consensus_tiles_shape",
                                "ref_shape": ref_m.shape,
                                "cand_shape": cand_m.shape
                            })
                        elif not np.array_equal(ref_m, cand_m):
                            mismatches.append({
                                "res": (W, H),
                                "scenario": scenario_name,
                                "phase": phase_name,
                                "shift": (dx, dy),
                                "type": "consensus_tiles_values"
                            })

    report = {
        "candidate": candidate_module_name,
        "reference": reference_module_name,
        "kind": kind,
        "seed": seed,
        "status": "PASS" if not mismatches else "FAIL",
        "mismatches_count": len(mismatches),
        "mismatches": mismatches,
        "tests_run": tests_run,
        "vm_timings": "VM never MEASURED"
    }
    return report


def main():
    parser = argparse.ArgumentParser(description="V2 Native Parity Checker")
    parser.add_argument("--kind", choices=["st", "gm"], required=True, help="Kind of plugin: st or gm")
    parser.add_argument("--candidate", required=True, help="Candidate module import path")
    parser.add_argument("--reference", help="Reference module import path")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")

    args = parser.parse_args()

    ref_module = args.reference
    if not ref_module:
        ref_module = "eval.v2proto_subtile" if args.kind == "st" else "eval.v2proto_global_motion"

    try:
        report = check(args.candidate, args.kind, ref_module, seed=args.seed)
    except Exception as exc:
        report = {
            "candidate": args.candidate,
            "reference": ref_module,
            "kind": args.kind,
            "seed": args.seed,
            "status": "FAIL",
            "error": str(exc),
            "vm_timings": "VM never MEASURED"
        }

    print(json.dumps(report, indent=2))
    if report.get("status") != "PASS":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
