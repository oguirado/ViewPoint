"""eval/v2x/adv_gm.py - Targeted adversarial DEV scene generator and scorer for global motion variants."""

import argparse
import importlib
import json
import sys
import traceback
import numpy as np


def compute_static_mask(prev, cur, s=16):
    """Return a boolean (gh, gw) array of tiles byte-identical to prev in place."""
    H, W = cur.shape
    gh = (H + s - 1) // s
    gw = (W + s - 1) // s
    pad_h = gh * s - H
    pad_w = gw * s - W
    if pad_h > 0 or pad_w > 0:
        p_prev = np.pad(prev, ((0, pad_h), (0, pad_w)), mode="constant", constant_values=0)
        p_cur = np.pad(cur, ((0, pad_h), (0, pad_w)), mode="constant", constant_values=1)
    else:
        p_prev = prev
        p_cur = cur
    diff = p_prev.reshape(gh, s, gw, s) != p_cur.reshape(gh, s, gw, s)
    return ~diff.any(axis=(1, 3))


def _make_texture(rng, h, w):
    return rng.randint(0, 256, size=(h, w), dtype=np.uint8)


def _make_periodic_texture(rng, h, w, period=16):
    row_pattern = rng.randint(0, 256, size=(period, w), dtype=np.uint8)
    reps = (h + period - 1) // period
    full = np.tile(row_pattern, (reps, 1))
    return full[:h, :w]


def cases(seed=0x5EED, include_1080p=True):
    """Generate deterministic case dicts for adversarial global motion evaluation."""
    rng = np.random.RandomState(seed)
    case_list = []

    # 1. flat_window_drag
    drag_specs = [
        ("flat_window_drag_0", 128, 128, 4, 4, 32, 32),
        ("flat_window_drag_1", 160, 160, 8, 8, 40, 40),
        ("flat_window_drag_2", 128, 160, 20, 0, 32, 40),
        ("flat_window_drag_3", 160, 128, 0, -12, 48, 32),
    ]
    for cid, h, w, dx, dy, y0, x0 in drag_specs:
        s = 16
        f0 = np.full((h, w), 100, dtype=np.uint8)
        f1 = np.full((h, w), 100, dtype=np.uint8)
        win_h, win_w = 64, 64
        tex = _make_texture(rng, win_h, win_w)
        f0[y0 : y0 + win_h, x0 : x0 + win_w] = tex
        f1[y0 + dy : y0 + dy + win_h, x0 + dx : x0 + dx + win_w] = tex
        m0 = compute_static_mask(f0, f1, s=s)
        case_list.append(
            {
                "case_id": cid,
                "kind": "flat_window_drag",
                "h": h,
                "w": w,
                "s": s,
                "frames": [f0, f1],
                "truth_vectors": [(dx, dy)],
                "static_masks": [m0],
            }
        )

    # 2. two_motions
    two_specs = [
        ("two_motions_0", 160, 200, (12, 0), (0, -10)),
        ("two_motions_1", 160, 200, (12, 0), (-12, 0)),
        ("two_motions_2", 160, 200, (0, 10), (0, -10)),
    ]
    for cid, h, w, (dx1, dy1), (dx2, dy2) in two_specs:
        s = 16
        f0 = np.full((h, w), 128, dtype=np.uint8)
        f1 = np.full((h, w), 128, dtype=np.uint8)
        obj_h, obj_w = 48, 48
        t1 = _make_texture(rng, obj_h, obj_w)
        t2 = _make_texture(rng, obj_h, obj_w)

        y1, x1 = 20, 20
        y2, x2 = 80, 100

        f0[y1 : y1 + obj_h, x1 : x1 + obj_w] = t1
        f1[y1 + dy1 : y1 + dy1 + obj_h, x1 + dx1 : x1 + dx1 + obj_w] = t1

        f0[y2 : y2 + obj_h, x2 : x2 + obj_w] = t2
        f1[y2 + dy2 : y2 + dy2 + obj_h, x2 + dx2 : x2 + dx2 + obj_w] = t2

        m0 = compute_static_mask(f0, f1, s=s)
        case_list.append(
            {
                "case_id": cid,
                "kind": "two_motions",
                "h": h,
                "w": w,
                "s": s,
                "frames": [f0, f1],
                "truth_vectors": [None],
                "static_masks": [m0],
            }
        )

    # 3. chrome_clock
    clock_specs = [
        ("chrome_clock_0", 256, 256, 48, 48),
        ("chrome_clock_1", 300, 300, 50, 50),
        ("chrome_clock_2", 240, 320, 40, 40),
    ]
    for cid, h, w, top_h, bot_h in clock_specs:
        s = 16
        dy = -24
        f0 = np.zeros((h, w), dtype=np.uint8)
        f1 = np.zeros((h, w), dtype=np.uint8)

        # Flat header & footer
        f0[:top_h] = 60
        f1[:top_h] = 60
        f0[h - bot_h :] = 60
        f1[h - bot_h :] = 60

        # Changing clock block in header
        f0[10:30, 10:50] = _make_texture(rng, 20, 40)
        f1[10:30, 10:50] = _make_texture(rng, 20, 40)

        # Middle scrolling band
        mh = h - top_h - bot_h
        mid_tex = _make_texture(rng, mh, w)
        f0[top_h : h - bot_h] = mid_tex
        f1[top_h : h - bot_h + dy] = mid_tex[-dy:]
        f1[h - bot_h + dy : h - bot_h] = _make_texture(rng, -dy, w)

        m0 = compute_static_mask(f0, f1, s=s)
        case_list.append(
            {
                "case_id": cid,
                "kind": "chrome_clock",
                "h": h,
                "w": w,
                "s": s,
                "frames": [f0, f1],
                "truth_vectors": [(0, dy)],
                "static_masks": [m0],
            }
        )

    # 4. periodic_text
    p_specs = [
        ("periodic_text_0", 256, 256, -19),
        ("periodic_text_1", 256, 256, -32),
        ("periodic_text_2", 320, 320, -19),
        ("periodic_text_3", 320, 320, -32),
    ]
    for cid, h, w, dy in p_specs:
        s = 16
        f0 = _make_periodic_texture(rng, h, w, period=16)
        f1 = np.zeros_like(f0)
        if dy < 0:
            f1[: h + dy] = f0[-dy:]
            f1[h + dy :] = f0[:-dy][: -dy]
        m0 = compute_static_mask(f0, f1, s=s)
        case_list.append(
            {
                "case_id": cid,
                "kind": "periodic_text",
                "h": h,
                "w": w,
                "s": s,
                "frames": [f0, f1],
                "truth_vectors": [None],
                "static_masks": [m0],
            }
        )

    # 5. bare_caret
    caret_specs = [
        ("bare_caret_0", 128, 128, 40, 40, 16),
        ("bare_caret_1", 160, 160, 50, 50, 16),
        ("bare_caret_2", 128, 160, 30, 60, 20),
    ]
    for cid, h, w, y0, x0, ch in caret_specs:
        s = 16
        dx = 8
        f0 = np.full((h, w), 200, dtype=np.uint8)
        f1 = np.full((h, w), 200, dtype=np.uint8)
        f0[y0 : y0 + ch, x0 : x0 + 2] = 0
        f1[y0 : y0 + ch, x0 + dx : x0 + dx + 2] = 0
        m0 = compute_static_mask(f0, f1, s=s)
        case_list.append(
            {
                "case_id": cid,
                "kind": "bare_caret",
                "h": h,
                "w": w,
                "s": s,
                "frames": [f0, f1],
                "truth_vectors": [(dx, 0)],
                "static_masks": [m0],
            }
        )

    # 6. big_scroll_1080p (optional / lazy)
    if include_1080p:
        h, w = 1088, 1920
        scroll_dys = [-600, -830, 600, 816]
        for idx, dy in enumerate(scroll_dys):
            s = 16
            f0 = _make_texture(rng, h, w)
            f1 = np.full((h, w), 50, dtype=np.uint8)
            if dy > 0:
                f1[dy:] = f0[:-dy]
            else:
                f1[: h + dy] = f0[-dy:]
            m0 = compute_static_mask(f0, f1, s=s)
            case_list.append(
                {
                    "case_id": f"big_scroll_1080p_{idx}",
                    "kind": "big_scroll_1080p",
                    "h": h,
                    "w": w,
                    "s": s,
                    "frames": [f0, f1],
                    "truth_vectors": [(0, dy)],
                    "static_masks": [m0],
                }
            )

    return case_list


def score(module_name_or_mod, seed=0x5EED, include_1080p=True):
    """Run imported global motion module on all cases and compute canonical stats."""
    try:
        if isinstance(module_name_or_mod, str):
            mod = importlib.import_module(module_name_or_mod)
        else:
            mod = module_name_or_mod
    except Exception as e:
        return {
            "status": "FAILED",
            "error": traceback.format_exc(),
            "kinds": {},
            "total": {
                "pairs": 0,
                "exact": 0,
                "silent": 0,
                "wrong_vectors": 0,
                "static_relabels": 0,
            },
        }

    try:
        all_cases = cases(seed=seed, include_1080p=include_1080p)
        kinds_stats = {}

        tot_pairs = 0
        tot_exact = 0
        tot_silent = 0
        tot_wrong = 0
        tot_relabels = 0

        for c in all_cases:
            kind = c["kind"]
            if kind not in kinds_stats:
                kinds_stats[kind] = {
                    "pairs": 0,
                    "exact": 0,
                    "silent": 0,
                    "wrong_vectors": 0,
                    "static_relabels": 0,
                }

            s = c.get("s", 16)
            frames = c["frames"]
            truth_vectors = c["truth_vectors"]
            static_masks = c["static_masks"]
            h, w = c["h"], c["w"]
            max_d = max(w, h) - 1

            for i in range(len(truth_vectors)):
                prev_luma = frames[i]
                cur_luma = frames[i + 1]
                truth = truth_vectors[i]
                static_mask = static_masks[i]

                res = mod.estimate_global_motion(prev_luma, cur_luma, max_d)

                if res is None:
                    est_vec = None
                    est_dx, est_dy = 0, 0
                else:
                    est_dx, est_dy = int(res[0]), int(res[1])
                    est_vec = (est_dx, est_dy)

                if res is not None and hasattr(mod, "consensus_tiles"):
                    consensus = mod.consensus_tiles(prev_luma, cur_luma, s, est_dx, est_dy)
                    consensus = np.asarray(consensus, dtype=bool)
                else:
                    gh = (h + s - 1) // s
                    gw = (w + s - 1) // s
                    consensus = np.zeros((gh, gw), dtype=bool)

                pairs = 1
                exact = 0
                silent = 0
                wrong_vectors = 0

                if truth is not None:
                    if est_vec == truth:
                        exact = 1
                    elif est_vec is None:
                        silent = 1
                    else:
                        wrong_vectors = 1
                else:
                    if kind == "periodic_text" and c["case_id"] in ("periodic_text_1", "periodic_text_3"):
                        if est_vec is None or est_vec == (0, 0):
                            exact = 1
                        else:
                            wrong_vectors = 1
                    else:
                        if est_vec is None:
                            exact = 1
                        else:
                            wrong_vectors = 1

                relabels = int(np.logical_and(consensus, static_mask).sum())

                ks = kinds_stats[kind]
                ks["pairs"] += pairs
                ks["exact"] += exact
                ks["silent"] += silent
                ks["wrong_vectors"] += wrong_vectors
                ks["static_relabels"] += relabels

                tot_pairs += pairs
                tot_exact += exact
                tot_silent += silent
                tot_wrong += wrong_vectors
                tot_relabels += relabels

        return {
            "status": "OK",
            "error": None,
            "kinds": kinds_stats,
            "total": {
                "pairs": tot_pairs,
                "exact": tot_exact,
                "silent": tot_silent,
                "wrong_vectors": tot_wrong,
                "static_relabels": tot_relabels,
            },
        }
    except Exception as e:
        return {
            "status": "FAILED",
            "error": traceback.format_exc(),
            "kinds": {},
            "total": {
                "pairs": 0,
                "exact": 0,
                "silent": 0,
                "wrong_vectors": 0,
                "static_relabels": 0,
            },
        }


def main():
    parser = argparse.ArgumentParser(description="Adversarial global motion evaluator")
    parser.add_argument("--variant", required=True, help="Module name (e.g. eval.v2x.gm_phasecorr)")
    parser.add_argument("--seed", type=int, default=0x5EED, help="Seed for case generator")
    parser.add_argument("--no-1080p", action="store_true", help="Exclude 1080p cases")
    args = parser.parse_args()

    result = score(args.variant, seed=args.seed, include_1080p=not args.no_1080p)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
