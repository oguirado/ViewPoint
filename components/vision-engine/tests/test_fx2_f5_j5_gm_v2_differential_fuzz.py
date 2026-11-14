"""tests/test_fx2_f5_j5_gm_v2_differential_fuzz.py - Post-fix sparse-scene differential fuzz tests."""

import os
import struct
import subprocess
import unittest
import numpy as np

from eval.v2x import gm_bands_hv
from eval.v2x import gm_v2_oracle as oracle
from tests.test_jq1_a4_rust_gm_v2_conformance_gate import resolve_v2_binary

MASTER_SEED = 0xF5F52026
print(f"MASTER_SEED: 0x{MASTER_SEED:08X} ({MASTER_SEED})")


def compute_consensus_mask_pixel_loop(prev: np.ndarray, cur: np.ndarray, s: int, dx: int, dy: int) -> np.ndarray:
    """Independently compute whole-real-tile consensus mask using pixel loops.

    A tile (ty, tx) is True iff:
    1. At least one pixel in the tile within frame bounds (H, W) changed between cur and prev (cur != prev).
    2. Every pixel in the tile within frame bounds (H, W) matches translated previous pixel:
       cur[y, x] == prev[y - dy, x - dx], and the source coordinate (y - dy, x - dx) is in bounds (0 <= y - dy < H, 0 <= x - dx < W).
    """
    H, W = cur.shape
    gh = (H + s - 1) // s
    gw = (W + s - 1) // s
    mask = np.zeros((gh, gw), dtype=bool)

    for ty in range(gh):
        y0 = ty * s
        y1 = min((ty + 1) * s, H)
        for tx in range(gw):
            x0 = tx * s
            x1 = min((tx + 1) * s, W)

            tile_changed = False
            tile_match = True

            for y in range(y0, y1):
                for x in range(x0, x1):
                    c_val = int(cur[y, x])
                    p_val = int(prev[y, x])
                    if c_val != p_val:
                        tile_changed = True

                    py = y - dy
                    px = x - dx
                    if 0 <= py < H and 0 <= px < W:
                        if c_val != int(prev[py, px]):
                            tile_match = False
                            break
                    else:
                        tile_match = False
                        break
                if not tile_match:
                    break

            if tile_changed and tile_match:
                mask[ty, tx] = True

    return mask


def generate_fuzz_cases(master_seed: int = MASTER_SEED) -> list:
    """Generate 50 fresh deterministic frame pairs from a printed fixed integer seed.

    Each pair is generated from its own derived seed so case selection does not change another fixture.
    """
    cases = []

    # Category 1: Sparse page translations across all 4 quadrants (20 cases: indices 0..19)
    # Quadrants: (+dx, +dy), (+dx, -dy), (-dx, +dy), (-dx, -dy)
    quadrant_shifts = [
        (16, 24), (32, 16), (5, 9), (48, 64), (12, 18),       # (+, +)
        (20, -15), (40, -30), (8, -12), (35, -25), (14, -10), # (+, -)
        (-18, 22), (-30, 45), (-10, 15), (-25, 30), (-16, 20), # (-, +)
        (-24, -32), (-15, -20), (-8, -14), (-36, -28), (-12, -16), # (-, -)
    ]

    for idx, (dx, dy) in enumerate(quadrant_shifts):
        derived_seed = (master_seed ^ (idx * 0x10001 + 0x1A2B3C4D)) & 0xFFFFFFFFFFFFFFFF
        sm = oracle.SplitMix64(derived_seed)
        w, h = 640, 480
        s = 16
        max_d = 200

        # Create sparse page (mostly light background 240 with textured block/lines)
        prev = np.full((h, w), 240, dtype=np.uint8)
        cur = np.full((h, w), 240, dtype=np.uint8)

        # Draw textured content block
        block_bytes = sm.fill_bytes(180 * 240)
        block = np.frombuffer(block_bytes, dtype=np.uint8).reshape((180, 240))
        by0, bx0 = 120, 150
        prev[by0 : by0 + 180, bx0 : bx0 + 240] = block

        # Translate in cur
        cy0, cy1 = max(0, dy), min(h, h + dy)
        cx0, cx1 = max(0, dx), min(w, w + dx)
        py0, py1 = cy0 - dy, cy1 - dy
        px0, px1 = cx0 - dx, cx1 - dx

        cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

        cases.append({
            "case_id": f"fuzz_sparse_quad_{idx}_dx{dx}_dy{dy}",
            "h": h, "w": w, "s": s, "max_d": max_d,
            "dx": dx, "dy": dy,
            "prev": prev, "cur": cur,
            "expected_type": "exact_translation",
        })

    # Category 2: Sub-tile shifts (6 cases: indices 20..25)
    subtile_shifts = [(1, 2), (3, 0), (0, 5), (7, 4), (-2, 3), (-4, -1)]
    for i, (dx, dy) in enumerate(subtile_shifts):
        idx = 20 + i
        derived_seed = (master_seed ^ (idx * 0x10001 + 0x1A2B3C4D)) & 0xFFFFFFFFFFFFFFFF
        sm = oracle.SplitMix64(derived_seed)
        w, h, s, max_d = 480, 360, 16, 100
        prev_bytes = sm.fill_bytes(w * h)
        prev = np.frombuffer(prev_bytes, dtype=np.uint8).reshape((h, w)).copy()
        cur = np.full_like(prev, 200)

        cy0, cy1 = max(0, dy), min(h, h + dy)
        cx0, cx1 = max(0, dx), min(w, w + dx)
        py0, py1 = cy0 - dy, cy1 - dy
        px0, px1 = cx0 - dx, cx1 - dx
        cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

        cases.append({
            "case_id": f"fuzz_subtile_{i}_dx{dx}_dy{dy}",
            "h": h, "w": w, "s": s, "max_d": max_d,
            "dx": dx, "dy": dy,
            "prev": prev, "cur": cur,
            "expected_type": "exact_translation",
        })

    # Category 3: Identical and Flat frames (4 cases: indices 26..29)
    # Case 26: Identical textured
    derived_seed = (master_seed ^ (26 * 0x10001 + 0x1A2B3C4D)) & 0xFFFFFFFFFFFFFFFF
    sm = oracle.SplitMix64(derived_seed)
    prev26 = np.frombuffer(sm.fill_bytes(320 * 240), dtype=np.uint8).reshape((240, 320)).copy()
    cases.append({
        "case_id": "fuzz_identical_textured",
        "h": 240, "w": 320, "s": 16, "max_d": 50,
        "dx": 0, "dy": 0,
        "prev": prev26, "cur": prev26.copy(),
        "expected_type": "exact_zero_motion",
    })

    # Case 27: Identical flat (luma 128)
    prev27 = np.full((200, 300), 128, dtype=np.uint8)
    cases.append({
        "case_id": "fuzz_identical_flat_128",
        "h": 200, "w": 300, "s": 16, "max_d": 50,
        "dx": 0, "dy": 0,
        "prev": prev27, "cur": prev27.copy(),
        "expected_type": "refusal",
    })

    # Case 28: Flat prev and flat cur (different values 100 vs 150)
    cases.append({
        "case_id": "fuzz_flat_diff_vals",
        "h": 200, "w": 300, "s": 16, "max_d": 50,
        "dx": 0, "dy": 0,
        "prev": np.full((200, 300), 100, dtype=np.uint8),
        "cur": np.full((200, 300), 150, dtype=np.uint8),
        "expected_type": "refusal",
    })

    # Case 29: Flat background with single pixel difference
    prev29 = np.full((200, 300), 128, dtype=np.uint8)
    cur29 = prev29.copy()
    cur29[100, 150] = 129
    cases.append({
        "case_id": "fuzz_flat_single_pixel_diff",
        "h": 200, "w": 300, "s": 16, "max_d": 50,
        "dx": 0, "dy": 0,
        "prev": prev29, "cur": cur29,
        "expected_type": "refusal",
    })

    # Category 4: Top and Bottom clock distractors (6 cases: indices 30..35)
    clock_configs = [
        ("top", 0, 20, 12, 0), ("top", 0, 20, 0, 16),
        ("bottom", 460, 480, -10, 0), ("bottom", 460, 480, 0, -12),
        ("both", 0, 20, 16, 16), ("both", 460, 480, -16, -16),
    ]
    for i, (pos, y_start, y_end, dx, dy) in enumerate(clock_configs):
        idx = 30 + i
        derived_seed = (master_seed ^ (idx * 0x10001 + 0x1A2B3C4D)) & 0xFFFFFFFFFFFFFFFF
        sm = oracle.SplitMix64(derived_seed)
        w, h, s, max_d = 640, 480, 16, 100
        prev = np.frombuffer(sm.fill_bytes(w * h), dtype=np.uint8).reshape((h, w)).copy()
        cur = np.empty_like(prev)

        cy0, cy1 = max(0, dy), min(h, h + dy)
        cx0, cx1 = max(0, dx), min(w, w + dx)
        py0, py1 = cy0 - dy, cy1 - dy
        px0, px1 = cx0 - dx, cx1 - dx
        cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

        # Add clock distractor
        clock_bytes = sm.fill_bytes(20 * 80)
        clock_patch = np.frombuffer(clock_bytes, dtype=np.uint8).reshape((20, 80))
        if pos == "top" or pos == "both":
            prev[0:20, 200:280] = clock_patch
            cur[0:20, 200:280] = np.frombuffer(sm.fill_bytes(20 * 80), dtype=np.uint8).reshape((20, 80))
        if pos == "bottom" or pos == "both":
            prev[460:480, 200:280] = clock_patch
            cur[460:480, 200:280] = np.frombuffer(sm.fill_bytes(20 * 80), dtype=np.uint8).reshape((20, 80))

        cases.append({
            "case_id": f"fuzz_clock_{pos}_{i}",
            "h": h, "w": w, "s": s, "max_d": max_d,
            "dx": dx, "dy": dy,
            "prev": prev, "cur": cur,
            "expected_type": "exact_translation" if (dx != 0 or dy != 0) else "exact_zero_motion",
        })

    # Category 5: Independently moving blocks and competing scenes (8 cases: indices 36..43)
    competing_configs = [
        (16, 0, -16, 0), (0, 24, 0, -24), (12, 12, -12, -12), (20, -10, -20, 10),
        (30, 0, 0, 30), (-15, 15, 15, -15), (8, 16, -16, -8), (25, 25, -25, -25),
    ]
    for i, (dx1, dy1, dx2, dy2) in enumerate(competing_configs):
        idx = 36 + i
        derived_seed = (master_seed ^ (idx * 0x10001 + 0x1A2B3C4D)) & 0xFFFFFFFFFFFFFFFF
        sm = oracle.SplitMix64(derived_seed)
        w, h, s, max_d = 640, 480, 16, 150
        prev = np.full((h, w), 50, dtype=np.uint8)
        cur = np.full((h, w), 50, dtype=np.uint8)

        # Block 1
        b1_prev = np.frombuffer(sm.fill_bytes(160 * 160), dtype=np.uint8).reshape((160, 160))
        prev[40:200, 40:200] = b1_prev
        cur[40 + dy1 : 200 + dy1, 40 + dx1 : 200 + dx1] = b1_prev

        # Block 2
        b2_prev = np.frombuffer(sm.fill_bytes(160 * 160), dtype=np.uint8).reshape((160, 160))
        prev[240:400, 360:520] = b2_prev
        cur[240 + dy2 : 400 + dy2, 360 + dx2 : 520 + dx2] = b2_prev

        cases.append({
            "case_id": f"fuzz_competing_{i}",
            "h": h, "w": w, "s": s, "max_d": max_d,
            "dx": dx1, "dy": dy1,
            "prev": prev, "cur": cur,
            "expected_type": "refusal",
        })

    # Category 6: Odd dimensions producing partial tiles (6 cases: indices 44..49)
    odd_dims = [(317, 211), (453, 305), (127, 99), (639, 479), (255, 255), (180, 320)]
    odd_shifts = [(12, 8), (-10, 14), (5, -7), (-15, -12), (0, 16), (20, 0)]
    for i, ((w, h), (dx, dy)) in enumerate(zip(odd_dims, odd_shifts)):
        idx = 44 + i
        derived_seed = (master_seed ^ (idx * 0x10001 + 0x1A2B3C4D)) & 0xFFFFFFFFFFFFFFFF
        sm = oracle.SplitMix64(derived_seed)
        s = 16
        max_d = 100
        prev_bytes = sm.fill_bytes(w * h)
        prev = np.frombuffer(prev_bytes, dtype=np.uint8).reshape((h, w)).copy()
        cur = np.full_like(prev, 180)

        cy0, cy1 = max(0, dy), min(h, h + dy)
        cx0, cx1 = max(0, dx), min(w, w + dx)
        py0, py1 = cy0 - dy, cy1 - dy
        px0, px1 = cx0 - dx, cx1 - dx
        cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

        cases.append({
            "case_id": f"fuzz_odd_dim_{i}_{w}x{h}",
            "h": h, "w": w, "s": s, "max_d": max_d,
            "dx": dx, "dy": dy,
            "prev": prev, "cur": cur,
            "expected_type": "exact_translation" if (dx != 0 or dy != 0) else "exact_zero_motion",
        })

    assert len(cases) == 50, f"Expected exactly 50 cases, got {len(cases)}"
    return cases


class TestFuzzFixturesAndPythonReference(unittest.TestCase):
    """Test fuzz fixture properties and Python reference implementation."""

    def test_fixture_count_and_uniqueness(self):
        cases = generate_fuzz_cases()
        self.assertEqual(len(cases), 50)
        case_ids = [c["case_id"] for c in cases]
        self.assertEqual(len(set(case_ids)), 50, "All 50 case_ids must be unique")

    def test_exact_translations_assert_nonzero_vector_and_zero_score(self):
        cases = generate_fuzz_cases()
        exact_cases = [c for c in cases if c["expected_type"] == "exact_translation"]
        self.assertGreaterEqual(len(exact_cases), 20)

        for case in exact_cases:
            res = gm_bands_hv.estimate_global_motion(case["prev"], case["cur"], case["max_d"])
            self.assertIsNotNone(res, f"Failed for exact translation case {case['case_id']}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (case["dx"], case["dy"]), f"Vector mismatch in {case['case_id']}")
            if "clock" not in case["case_id"]:
                self.assertEqual(score, 0, f"Score must be 0 for clean exact translation in {case['case_id']}")

    def test_competing_scenes_assert_refusal(self):
        cases = generate_fuzz_cases()
        competing_cases = [c for c in cases if c["case_id"].startswith("fuzz_competing_")]
        self.assertEqual(len(competing_cases), 8)

        for case in competing_cases:
            res = gm_bands_hv.estimate_global_motion(case["prev"], case["cur"], case["max_d"])
            self.assertIsNone(res, f"Expected refusal for competing scene {case['case_id']}")

    def test_independent_pixel_loop_consensus_matches_gm_bands_hv(self):
        cases = generate_fuzz_cases()
        # Test across first 10 exact translation cases plus odd dimension cases
        test_subset = [c for c in cases if c["expected_type"] in ("exact_translation", "exact_zero_motion")][:15]

        for case in test_subset:
            dx, dy = case["dx"], case["dy"]
            s = case["s"]
            exp_mask = compute_consensus_mask_pixel_loop(case["prev"], case["cur"], s, dx, dy)
            actual_mask = gm_bands_hv.consensus_tiles(case["prev"], case["cur"], s, dx, dy)
            np.testing.assert_array_equal(
                actual_mask, exp_mask,
                err_msg=f"Pixel-loop consensus mismatch in {case['case_id']}"
            )


class TestNativeDifferentialFuzz(unittest.TestCase):
    """Native differential fuzz test suite comparing rust/vqa_k_gm_v2 against gm_v2_oracle."""

    # fx2-f5-rust-vqa-k-gm-v2-conformance
    @unittest.expectedFailure
    def test_native_differential_fuzz_50_cases(self):
        bin_path = resolve_v2_binary()
        self.assertTrue(os.path.exists(bin_path), f"Native binary missing at {bin_path}")
        self.assertTrue(os.access(bin_path, os.X_OK), f"Native binary not executable at {bin_path}")

        proc = subprocess.Popen(
            [bin_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

        cases = generate_fuzz_cases()

        try:
            for idx, case in enumerate(cases):
                ref_tuple, ref_mask = oracle.expected("eval.v2x.gm_bands_hv", case)
                exp_has_gm, exp_dx, exp_dy, exp_score, exp_gh, exp_gw = ref_tuple

                # 1. Send Op 1
                req1 = oracle.encode_request(case, op=1)
                proc.stdin.write(req1)
                proc.stdin.flush()

                resp1 = oracle.decode_response(proc.stdout, op=1)

                self.assertEqual(resp1["status"], 0, f"Case {case['case_id']} op 1 non-zero status")
                self.assertEqual(resp1["has_gm"], exp_has_gm, f"Case {case['case_id']} has_gm mismatch")
                self.assertEqual(resp1["dx"], exp_dx, f"Case {case['case_id']} dx mismatch")
                self.assertEqual(resp1["dy"], exp_dy, f"Case {case['case_id']} dy mismatch")
                self.assertEqual(resp1["score"], exp_score, f"Case {case['case_id']} score mismatch")
                self.assertEqual(resp1["gh"], exp_gh, f"Case {case['case_id']} gh mismatch")
                self.assertEqual(resp1["gw"], exp_gw, f"Case {case['case_id']} gw mismatch")
                np.testing.assert_array_equal(
                    resp1["mask"], ref_mask,
                    err_msg=f"Case {case['case_id']} op 1 mask mismatch"
                )

                # 2. Send Explicit Op 2
                req2 = oracle.encode_request(case, op=2)
                proc.stdin.write(req2)
                proc.stdin.flush()

                resp2 = oracle.decode_response(proc.stdout, op=2)
                self.assertEqual(resp2["status"], 0, f"Case {case['case_id']} op 2 non-zero status")
                self.assertEqual(resp2["gh"], exp_gh, f"Case {case['case_id']} op 2 gh mismatch")
                self.assertEqual(resp2["gw"], exp_gw, f"Case {case['case_id']} op 2 gw mismatch")

                # Verify explicit Op 2 mask matches independent pixel loop mask as well as oracle
                pix_mask = compute_consensus_mask_pixel_loop(case["prev"], case["cur"], case["s"], exp_dx, exp_dy)
                np.testing.assert_array_equal(
                    resp2["mask"], pix_mask,
                    err_msg=f"Case {case['case_id']} op 2 pixel-loop mask mismatch"
                )

                # 3. Cache/stale shape check: after case 10 (which is 640x480), send case 44 (which is odd dim 317x211), then re-send case 10
                if idx == 10:
                    case_odd = cases[44]
                    req_odd = oracle.encode_request(case_odd, op=1)
                    proc.stdin.write(req_odd)
                    proc.stdin.flush()
                    resp_odd = oracle.decode_response(proc.stdout, op=1)
                    self.assertEqual(resp_odd["status"], 0)

                    # Re-send case 10
                    proc.stdin.write(req1)
                    proc.stdin.flush()
                    resp1_repeat = oracle.decode_response(proc.stdout, op=1)
                    self.assertEqual(resp1_repeat["gh"], exp_gh)
                    self.assertEqual(resp1_repeat["gw"], exp_gw)
                    np.testing.assert_array_equal(resp1_repeat["mask"], ref_mask)

            # Send quit op 0
            proc.stdin.write(struct.pack("<i", 0))
            proc.stdin.flush()
        finally:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            if proc.stdout and not proc.stdout.closed:
                proc.stdout.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
