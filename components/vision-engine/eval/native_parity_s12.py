"""Host instrument that evaluates native implementations of S1 and S2 kernels against Python references."""

import argparse
import importlib.util
import json
import sys
import numpy as np

from visioncore import change, desc, grid as vgrid

NAMES = [
    "_tile_sad_windows",
    "_tile_sad",
    "_tile_sad_clip",
    "global_vector",
    "_tile_sums",
    "_islands",
    "island_vector",
    "describe_padded_tiles",
]

DEFAULT_SIZES = [(16, 16), (63, 45), (320, 180), (1920, 1080)]


def _ref_describe_padded_tiles(padded, grid):
    tiles, _ = desc.describe_padded(padded, grid)
    return tiles


def _check_equal(a, b):
    if type(a) != type(b):
        if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
            if len(a) != len(b):
                return False
            return all(_check_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, tuple) and isinstance(b, tuple):
        if len(a) != len(b):
            return False
        return all(_check_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, np.ndarray) and isinstance(b, np.ndarray):
        if a.shape != b.shape or a.dtype != b.dtype:
            return False
        return np.array_equal(a, b)
    if isinstance(a, (int, np.integer)) and isinstance(b, (int, np.integer)):
        return int(a) == int(b)
    if isinstance(a, (bool, np.bool_)) and isinstance(b, (bool, np.bool_)):
        return bool(a) == bool(b)
    return a == b


def _generate_planes(rng, h, w, plane_type):
    if plane_type == "flat":
        p1 = np.full((h, w), 128, dtype=np.int16)
        p2 = np.full((h, w), 128, dtype=np.int16)
    elif plane_type == "random":
        p1 = rng.integers(-32768, 32767, (h, w), dtype=np.int16)
        p2 = rng.integers(-32768, 32767, (h, w), dtype=np.int16)
    elif plane_type == "shifted-copy":
        p1 = rng.integers(-1000, 1000, (h, w), dtype=np.int16)
        p2 = np.roll(p1, shift=(2, -3), axis=(0, 1))
    elif plane_type == "saturated":
        p1 = rng.choice(np.array([0, 255], dtype=np.int16), size=(h, w))
        p2 = rng.choice(np.array([0, 255], dtype=np.int16), size=(h, w))
    else:
        raise ValueError(f"Unknown plane_type: {plane_type}")
    return p1, p2


def _generate_rgb_planes(rng, h, w, plane_type):
    if plane_type == "flat":
        p1 = np.full((h, w, 3), 128, dtype=np.uint8)
    elif plane_type == "random":
        p1 = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    elif plane_type == "shifted-copy":
        p1 = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
        p1 = np.roll(p1, shift=(2, -3, 0), axis=(0, 1, 2))
    elif plane_type == "saturated":
        p1 = rng.choice(np.array([0, 255], dtype=np.uint8), size=(h, w, 3))
    else:
        raise ValueError(f"Unknown plane_type: {plane_type}")
    return p1


def _generate_test_cases_for_name(name, seed, sizes):
    rng = np.random.default_rng(seed)
    cases = []
    plane_types = ["flat", "random", "shifted-copy", "saturated"]

    for W, H in sizes:
        for s in [16, 32]:
            gw = max(1, (W + s - 1) // s)
            gh = max(1, (H + s - 1) // s)
            frame_w = gw * s
            frame_h = gh * s

            for plane_type in plane_types:
                if name in ("_tile_sad_windows", "_tile_sad", "_tile_sad_clip"):
                    y_cur, y_prev = _generate_planes(rng, frame_h, frame_w, plane_type)
                    shifts = [(0, 0), (2, -2), (s, -s), (frame_w, frame_h)]
                    # Boundary tx and ty coordinates
                    tx = np.array([0, gw - 1, -1, -gw, 0], dtype=np.int64)
                    ty = np.array([0, gh - 1, -1, -gh, gh - 1], dtype=np.int64)

                    for c in shifts:
                        desc_str = f"{name} size={W}x{H} s={s} plane={plane_type} shift={c}"
                        if name == "_tile_sad_windows":
                            cases.append((desc_str, change._tile_sad_windows, (y_cur, y_prev, c, s, tx, ty)))
                        elif name == "_tile_sad":
                            cases.append((desc_str, change._tile_sad, (y_cur, y_prev, c, s, gh, gw, tx, ty)))
                        elif name == "_tile_sad_clip":
                            cases.append((desc_str, change._tile_sad_clip, (y_cur, y_prev, c, s, gh, gw, tx, ty)))

                elif name == "global_vector":
                    y_cur, y_prev = _generate_planes(rng, frame_h, frame_w, plane_type)
                    desc_str = f"global_vector size={W}x{H} s={s} plane={plane_type}"
                    cases.append((desc_str, change.global_vector, (y_cur, y_prev, s)))

                elif name == "_tile_sums":
                    absdiff = rng.integers(0, 512, (frame_h, frame_w), dtype=np.int64)
                    desc_str = f"_tile_sums size={W}x{H} s={s}"
                    cases.append((desc_str, change._tile_sums, (absdiff, s, gh, gw)))

                elif name == "_islands":
                    mask = rng.choice([True, False], size=(gh, gw))
                    desc_str = f"_islands size={W}x{H} s={s} plane={plane_type}"
                    cases.append((desc_str, change._islands, (mask,)))

                elif name == "island_vector":
                    y_cur, y_prev = _generate_planes(rng, frame_h, frame_w, plane_type)
                    mask = rng.choice([True, False], size=(gh, gw))
                    if not mask.any():
                        mask[0, 0] = True
                    desc_str = f"island_vector size={W}x{H} s={s} plane={plane_type}"
                    cases.append((desc_str, change.island_vector, (mask, y_cur, y_prev, s)))

                elif name == "describe_padded_tiles":
                    rgb = _generate_rgb_planes(rng, H, W, plane_type)
                    g = vgrid.Grid(W, H)
                    padded = g.pad(rgb)
                    desc_str = f"describe_padded_tiles size={W}x{H} plane={plane_type}"
                    cases.append((desc_str, _ref_describe_padded_tiles, (padded, g)))

    return cases


def parity(candidate, seed=0x5EED, sizes=None):
    if sizes is None:
        sizes = DEFAULT_SIZES

    report = {
        "status": "OK",
        "seed": seed,
        "timing": "VM never MEASURED",
        "results": {},
    }

    all_passed = True

    for name in NAMES:
        if not hasattr(candidate, name):
            report["results"][name] = {
                "status": "ABSENT",
                "cases": 0,
                "equal": 0,
                "first_mismatch": "ABSENT",
            }
            all_passed = False
            continue

        cand_fn = getattr(candidate, name)
        test_cases = _generate_test_cases_for_name(name, seed, sizes)

        total_cases = len(test_cases)
        equal_cases = 0
        first_mismatch = None

        for case_desc, ref_fn, args in test_cases:
            ref_res = ref_fn(*args)
            try:
                cand_res = cand_fn(*args)
            except Exception as e:
                first_mismatch = f"{case_desc} Exception: {e}"
                break

            if _check_equal(ref_res, cand_res):
                equal_cases += 1
            else:
                if first_mismatch is None:
                    first_mismatch = f"Mismatch in {case_desc}"
                break

        if equal_cases == total_cases and total_cases > 0:
            report["results"][name] = {
                "status": "OK",
                "cases": total_cases,
                "equal": equal_cases,
                "first_mismatch": None,
            }
        else:
            report["results"][name] = {
                "status": "MISMATCH",
                "cases": total_cases,
                "equal": equal_cases,
                "first_mismatch": first_mismatch or "Unknown mismatch",
            }
            all_passed = False

    if not all_passed:
        report["status"] = "FAIL"

    return report


def load_candidate(module_name=None, so_path=None):
    if module_name and not so_path:
        return importlib.import_module(module_name)
    elif so_path:
        name = module_name or "native_parity_candidate"
        spec = importlib.util.spec_from_file_location(name, so_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load library from {so_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    else:
        raise ValueError("Either --module or --so must be provided")


def main():
    parser = argparse.ArgumentParser(description="Native Parity S1/S2 Host Instrument")
    parser.add_argument("--module", type=str, help="Dotted python module name")
    parser.add_argument("--so", type=str, help="Path to .so library")
    parser.add_argument("--seed", type=int, default=0x5EED, help="PRNG seed for test fixtures")

    args = parser.parse_args()

    if not args.module and not args.so:
        parser.error("Either --module or --so is required")

    try:
        candidate = load_candidate(args.module, args.so)
    except Exception as e:
        err_report = {
            "status": "FAIL",
            "timing": "VM never MEASURED",
            "error": str(e),
        }
        print(json.dumps(err_report, indent=2))
        sys.exit(1)

    rep = parity(candidate, seed=args.seed)
    print(json.dumps(rep, indent=2))

    if rep["status"] != "OK":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
