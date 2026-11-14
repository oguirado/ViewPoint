"""tests/test_p1_scrollmid_matrix.py - Acceptance matrix for Problem P1 (scrolling middle band with static header/footer)."""

import importlib
import json
import math
import os
import sys
import unittest
import numpy as np

# Ground-truth table of expected outcomes (detect / miss) for problem P1.
# Documented limitations per variant and scene.
KNOWN_P1 = {
    # v2.1 common-band evidence and exact viewport certification (90de884, 8524da6).
    "gm_bands_hv": {
        "scrollmid_h32_dy1": "detect",
        "scrollmid_h32_dy5": "detect",
        "scrollmid_h32_dy-9": "detect",
        "scrollmid_h32_dy16": "detect",
        "scrollmid_h64_dy1": "detect",
        "scrollmid_h64_dy5": "detect",
        "scrollmid_h64_dy-9": "detect",
        "scrollmid_h64_dy16": "detect",
        "scrollmid_h96_dy1": "miss",
        "scrollmid_h96_dy5": "miss",
        "scrollmid_h96_dy-9": "miss",
        "scrollmid_h96_dy16": "miss",
        "control_full_dy1": "detect",
        "control_full_dy5": "detect",
        "control_full_dy-9": "detect",
        "control_full_dy16": "detect",
        "control_static": "detect",
    },
    "gm_corners": {
        "scrollmid_h32_dy1": "detect",
        "scrollmid_h32_dy5": "miss",
        "scrollmid_h32_dy-9": "miss",
        "scrollmid_h32_dy16": "miss",
        "scrollmid_h64_dy1": "miss",
        "scrollmid_h64_dy5": "miss",
        "scrollmid_h64_dy-9": "miss",
        "scrollmid_h64_dy16": "miss",
        "scrollmid_h96_dy1": "miss",
        "scrollmid_h96_dy5": "miss",
        "scrollmid_h96_dy-9": "miss",
        "scrollmid_h96_dy16": "miss",
        "control_full_dy1": "detect",
        "control_full_dy5": "detect",
        "control_full_dy-9": "detect",
        "control_full_dy16": "detect",
        "control_static": "detect",
    },
    "gm_gradproj": {
        "scrollmid_h32_dy1": "miss",
        "scrollmid_h32_dy5": "miss",
        "scrollmid_h32_dy-9": "miss",
        "scrollmid_h32_dy16": "miss",
        "scrollmid_h64_dy1": "miss",
        "scrollmid_h64_dy5": "miss",
        "scrollmid_h64_dy-9": "miss",
        "scrollmid_h64_dy16": "miss",
        "scrollmid_h96_dy1": "miss",
        "scrollmid_h96_dy5": "miss",
        "scrollmid_h96_dy-9": "miss",
        "scrollmid_h96_dy16": "miss",
        "control_full_dy1": "miss",
        "control_full_dy5": "miss",
        "control_full_dy-9": "miss",
        "control_full_dy16": "miss",
        "control_static": "miss",
    },
    "gm_multihyp": {
        "scrollmid_h32_dy1": "detect",
        "scrollmid_h32_dy5": "detect",
        "scrollmid_h32_dy-9": "detect",
        "scrollmid_h32_dy16": "detect",
        "scrollmid_h64_dy1": "detect",
        "scrollmid_h64_dy5": "detect",
        "scrollmid_h64_dy-9": "miss",
        "scrollmid_h64_dy16": "miss",
        "scrollmid_h96_dy1": "miss",
        "scrollmid_h96_dy5": "miss",
        "scrollmid_h96_dy-9": "miss",
        "scrollmid_h96_dy16": "miss",
        "control_full_dy1": "detect",
        "control_full_dy5": "detect",
        "control_full_dy-9": "detect",
        "control_full_dy16": "detect",
        "control_static": "detect",
    },
    "gm_phasecorr": {
        "scrollmid_h32_dy1": "detect",
        "scrollmid_h32_dy5": "detect",
        "scrollmid_h32_dy-9": "detect",
        "scrollmid_h32_dy16": "detect",
        "scrollmid_h64_dy1": "detect",
        "scrollmid_h64_dy5": "detect",
        "scrollmid_h64_dy-9": "detect",
        "scrollmid_h64_dy16": "detect",
        "scrollmid_h96_dy1": "miss",
        "scrollmid_h96_dy5": "miss",
        "scrollmid_h96_dy-9": "miss",
        "scrollmid_h96_dy16": "miss",
        "control_full_dy1": "miss",
        "control_full_dy5": "miss",
        "control_full_dy-9": "detect",
        "control_full_dy16": "miss",
        "control_static": "detect",
    },
    "gm_rowhash": {
        "scrollmid_h32_dy1": "detect",
        "scrollmid_h32_dy5": "detect",
        "scrollmid_h32_dy-9": "miss",
        "scrollmid_h32_dy16": "miss",
        "scrollmid_h64_dy1": "detect",
        "scrollmid_h64_dy5": "miss",
        "scrollmid_h64_dy-9": "miss",
        "scrollmid_h64_dy16": "miss",
        "scrollmid_h96_dy1": "miss",
        "scrollmid_h96_dy5": "miss",
        "scrollmid_h96_dy-9": "miss",
        "scrollmid_h96_dy16": "miss",
        "control_full_dy1": "detect",
        "control_full_dy5": "detect",
        "control_full_dy-9": "detect",
        "control_full_dy16": "detect",
        "control_static": "detect",
    },
    "gm_vechist": {
        "scrollmid_h32_dy1": "detect",
        "scrollmid_h32_dy5": "detect",
        "scrollmid_h32_dy-9": "miss",
        "scrollmid_h32_dy16": "miss",
        "scrollmid_h64_dy1": "detect",
        "scrollmid_h64_dy5": "miss",
        "scrollmid_h64_dy-9": "miss",
        "scrollmid_h64_dy16": "miss",
        "scrollmid_h96_dy1": "miss",
        "scrollmid_h96_dy5": "miss",
        "scrollmid_h96_dy-9": "miss",
        "scrollmid_h96_dy16": "miss",
        "control_full_dy1": "detect",
        "control_full_dy5": "detect",
        "control_full_dy-9": "miss",
        "control_full_dy16": "miss",
        "control_static": "detect",
    },
}


def make_text_canvas(width, height, seed=0x5EED):
    """Generate a synthetic text-like luma canvas in RAM with fixed seed."""
    rng = np.random.default_rng(seed)
    frame = np.full((height, width), 240, dtype=np.uint8)
    for y in range(20, height - 20, 25):
        x = 20
        while x < width - 40:
            gw = int(rng.integers(5, 25))
            gh = int(rng.integers(10, 18))
            frame[y : y + gh, x : x + gw] = rng.integers(20, 60, dtype=np.uint8)
            x += gw + int(rng.integers(4, 12))
    return frame


def build_p1_scenes(width=640, height=360):
    """Build problem P1 test scenes in RAM with fixed NumPy seed 0x5EED.

    Scenes include:
    - 12 scrolling middle scenes: header/footer heights 32, 64, 96; dy = 1, 5, -9, 16.
    - 4 full-frame scroll controls: dy = 1, 5, -9, 16.
    - 1 pure static pair control: dy = 0.
    """
    canvas = make_text_canvas(width, height + 200, seed=0x5EED)
    scenes = []

    # 12 scrolling middle scenes
    for h in [32, 64, 96]:
        for dy in [1, 5, -9, 16]:
            scenes.append({
                "name": f"scrollmid_h{h}_dy{dy}",
                "h_top": h,
                "h_bot": h,
                "dy": dy,
                "true_dx": 0,
                "true_dy": dy,
            })

    # 4 full-frame scroll controls
    for dy in [1, 5, -9, 16]:
        scenes.append({
            "name": f"control_full_dy{dy}",
            "h_top": 0,
            "h_bot": 0,
            "dy": dy,
            "true_dx": 0,
            "true_dy": dy,
        })

    # 1 pure static control
    scenes.append({
        "name": "control_static",
        "h_top": 0,
        "h_bot": 0,
        "dy": 0,
        "true_dx": 0,
        "true_dy": 0,
    })

    # Render frames in RAM
    for sc in scenes:
        h_top = sc["h_top"]
        h_bot = sc["h_bot"]
        dy = sc["dy"]

        prev = canvas[50 : 50 + height, :].copy()
        cur = canvas[50 - dy : 50 - dy + height, :].copy()

        if h_top > 0:
            prev[:h_top, :] = canvas[:h_top, :]
            cur[:h_top, :] = canvas[:h_top, :]
        if h_bot > 0:
            prev[height - h_bot :, :] = canvas[height - h_bot : height, :]
            cur[height - h_bot :, :] = canvas[height - h_bot : height, :]

        sc["prev"] = prev
        sc["cur"] = cur

    return scenes


class TestP1ScrollMidMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.width = 640
        cls.height = 360
        cls.tile_s = 16
        cls.gh = math.ceil(cls.height / cls.tile_s)
        cls.gw = math.ceil(cls.width / cls.tile_s)
        cls.max_d = max(cls.width, cls.height) - 1

        # Discover all gm_ variant modules in eval/v2x at test time
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        v2x_dir = os.path.join(repo_root, "eval", "v2x")
        cls.variant_names = sorted([
            f[:-3]
            for f in os.listdir(v2x_dir)
            if f.startswith("gm_") and f.endswith(".py") and f != "gm_v2_oracle.py"
        ])

        cls.scenes = build_p1_scenes(cls.width, cls.height)
        cls.matrix = cls._evaluate_matrix()

        # Print the matrix as one JSON line to stdout for the host
        json_line = json.dumps(cls.matrix, sort_keys=True)
        print(json_line, file=sys.stdout)

    @classmethod
    def _evaluate_matrix(cls):
        matrix = {}
        for vname in cls.variant_names:
            mod = importlib.import_module(f"eval.v2x.{vname}")

            # Verify presence of required API functions
            assert hasattr(mod, "estimate_global_motion"), f"{vname} missing estimate_global_motion"
            assert hasattr(mod, "consensus_tiles"), f"{vname} missing consensus_tiles"

            v_results = {}
            for sc in cls.scenes:
                prev = sc["prev"]
                cur = sc["cur"]
                true_dx = sc["true_dx"]
                true_dy = sc["true_dy"]

                res = mod.estimate_global_motion(prev, cur, max_d=cls.max_d)

                # Validate API contract
                if res is not None:
                    assert isinstance(res, (tuple, list)), f"res must be tuple/list in {vname}"
                    assert len(res) == 3, f"res must be length 3 in {vname}"
                    dx_est, dy_est, score = res
                    assert isinstance(dx_est, (int, np.integer)), f"dx_est not int in {vname}"
                    assert isinstance(dy_est, (int, np.integer)), f"dy_est not int in {vname}"
                    assert isinstance(score, (int, np.integer)), f"score not int in {vname}"
                    assert score >= 0, f"score must be >= 0 in {vname}"
                    assert abs(dx_est) <= cls.max_d, f"dx out of range in {vname}"
                    assert abs(dy_est) <= cls.max_d, f"dy out of range in {vname}"

                    matched = (int(dx_est) == true_dx and int(dy_est) == true_dy)
                    c_dx, c_dy = int(dx_est), int(dy_est)
                else:
                    matched = False
                    c_dx, c_dy = true_dx, true_dy

                mask = mod.consensus_tiles(prev, cur, cls.tile_s, c_dx, c_dy)
                assert isinstance(mask, np.ndarray), f"mask not ndarray in {vname}"
                assert mask.dtype == bool, f"mask dtype not bool in {vname}"
                assert mask.shape == (cls.gh, cls.gw), f"mask shape invalid in {vname}"

                # Calculate share of middle-band tiles claimed
                h_top, h_bot = sc["h_top"], sc["h_bot"]
                mid_rows = [
                    ty for ty in range(cls.gh)
                    if (ty * cls.tile_s) >= h_top and ((ty + 1) * cls.tile_s) <= (cls.height - h_bot)
                ]

                if mid_rows:
                    mid_share = float(np.mean(mask[mid_rows, :]))
                else:
                    mid_share = float(np.mean(mask))

                outcome = "detect" if matched else "miss"
                v_results[sc["name"]] = {
                    "outcome": outcome,
                    "estimated_vector": list(res[:2]) if res is not None else None,
                    "true_vector": [true_dx, true_dy],
                    "middle_band_tile_share": round(mid_share, 4),
                }

            matrix[vname] = v_results
        return matrix

    def test_discovered_variants_match_known_table(self):
        """Every pinned variant must still be discovered; new variants are reported, not failed (the pin table grows by review)."""
        for vname in KNOWN_P1:
            self.assertIn(vname, self.variant_names, f"Pinned variant {vname} disappeared")
        new = sorted(set(self.variant_names) - set(KNOWN_P1))
        if new:
            print(f"P1 matrix: new variants not yet pinned: {new}: { {v: {k: d['outcome'] for k, d in self.matrix[v].items()} for v in new} }")

    def test_matrix_outcomes_match_known_p1(self):
        """Assert that observed detect/miss outcomes match KNOWN_P1 exactly."""
        for vname, sc_results in self.matrix.items():
            if vname not in KNOWN_P1:
                continue   # reported by the discovery test; pinned after review
            for sc_name, details in sc_results.items():
                observed = details["outcome"]
                expected = KNOWN_P1[vname][sc_name]
                self.assertEqual(
                    observed,
                    expected,
                    f"Outcome mismatch for variant '{vname}' on scene '{sc_name}': "
                    f"observed '{observed}', expected '{expected}'",
                )


if __name__ == "__main__":
    unittest.main()
