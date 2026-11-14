"""tests/test_jq1_a1_gm_constellation_voter_prototype.py - Constellation voter research prototype test.

This test module contains an isolated prototype implementation of the constellation
voter global motion estimator. eval/v2x/gm_constellation.py is absent and must not be created.
"""

import ast
import inspect
import types
import unittest
import numpy as np

import eval.v2x.adv_gm as adv_gm

# Prototype Parameters
# Top-K bounded set of high-gradient 16x16 luma patches selected per frame.
K_TOP_PATCHES = 64
# Minimum informative vote support count required to consider a primary vector candidate.
MIN_INFORMATIVE_SUPPORT = 4


def estimate_global_motion(prev_luma, cur_luma, max_d):
    """Estimate global integer displacement (dx, dy) between prev_luma and cur_luma.

    Uses a deterministic constellation patch voter prototype. Returns (dx, dy, score) tuple
    of Python ints or None.
    """
    prev = np.asarray(prev_luma, dtype=np.uint8)
    cur = np.asarray(cur_luma, dtype=np.uint8)
    H, W = cur.shape
    if H < 16 or W < 16:
        return None

    s = 16
    patches = []
    for r in range(0, H - s + 1, 16):
        for c in range(0, W - s + 1, 16):
            p = prev[r:r + s, c:c + s]
            dh = np.abs(p[:, 1:].astype(np.int64) - p[:, :-1].astype(np.int64)).sum()
            dv = np.abs(p[1:, :].astype(np.int64) - p[:-1, :].astype(np.int64)).sum()
            grad = int(dh + dv)
            if grad > 0:
                patches.append((grad, r, c, p))

    if not patches:
        return None

    # Deterministic tie-breaks: higher gradient score, then lower row, then lower col
    patches.sort(key=lambda item: (item[0], -item[1], -item[2]), reverse=True)
    top_k = patches[:K_TOP_PATCHES]

    # Pre-compute integral image over cur_luma for fast sum matching
    cur_i64 = cur.astype(np.int64)
    integ = np.zeros((H + 1, W + 1), dtype=np.int64)
    integ[1:, 1:] = np.cumsum(np.cumsum(cur_i64, axis=0), axis=1)
    all_sums = integ[s:, s:] - integ[:-s, s:] - integ[s:, :-s] + integ[:-s, :-s]

    votes = {}
    for grad, r_p, c_p, p_mat in top_k:
        p_sum = int(p_mat.sum())
        matches = np.argwhere(all_sums == p_sum)
        for r_c, c_c in matches:
            r_c = int(r_c)
            c_c = int(c_c)
            dx = int(c_c - c_p)
            dy = int(r_c - r_p)
            if abs(dx) <= max_d and abs(dy) <= max_d:
                if np.array_equal(cur[r_c:r_c + s, c_c:c_c + s], p_mat):
                    cand = (dx, dy)
                    votes[cand] = votes.get(cand, 0) + 1

    if not votes:
        return None

    sorted_votes = sorted(
        votes.items(),
        key=lambda item: (item[1], -abs(item[0][0]) - abs(item[0][1]), -item[0][0], -item[0][1]),
        reverse=True,
    )
    best_vec, best_count = sorted_votes[0]

    # Check minimum informative support requirement
    if best_count < MIN_INFORMATIVE_SUPPORT:
        return None

    # Reject exact ties for top vote count
    if len(sorted_votes) > 1 and sorted_votes[1][1] == best_count:
        return None

    best_dx, best_dy = best_vec

    # Non-adjacent runner-up check (distance >= 4 in dx or dy)
    non_adj = [
        count for vec, count in sorted_votes[1:]
        if max(abs(vec[0] - best_dx), abs(vec[1] - best_dy)) >= 4
    ]

    if non_adj:
        runner_up = max(non_adj)
        if best_count <= 3 * runner_up:
            return None

    # Verify at least 600-ppt (60.0%) changed-tile match across whole-frame tile grid
    cy0, cy1 = max(0, best_dy), min(H, H + best_dy)
    cx0, cx1 = max(0, best_dx), min(W, W + best_dx)
    if cy0 >= cy1 or cx0 >= cx1:
        return None

    gh = (H + s - 1) // s
    gw = (W + s - 1) // s

    changed_tiles = 0
    matched_tiles = 0

    for gy in range(gh):
        for gx in range(gw):
            y0, y1 = gy * s, min((gy + 1) * s, H)
            x0, x1 = gx * s, min((gx + 1) * s, W)

            cb = cur[y0:y1, x0:x1]
            pb = prev[y0:y1, x0:x1]

            if np.array_equal(cb, pb):
                continue

            changed_tiles += 1

            if y0 >= cy0 and y1 <= cy1 and x0 >= cx0 and x1 <= cx1:
                sb = prev[y0 - best_dy:y1 - best_dy, x0 - best_dx:x1 - best_dx]
                if np.array_equal(cb, sb):
                    matched_tiles += 1

    if changed_tiles == 0 or matched_tiles * 1000 < changed_tiles * 600:
        return None

    c_sub = cur[cy0:cy1, cx0:cx1].astype(np.int64)
    p_sub = prev[cy0 - best_dy:cy1 - best_dy, cx0 - best_dx:cx1 - best_dx].astype(np.int64)
    sad = int(np.abs(c_sub - p_sub).sum())

    return (best_dx, best_dy, sad)


def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
    """Return a boolean (gh, gw) numpy array of tiles whose whole destination tile

    differs in place and equals the in-bounds translated previous pixels.
    """
    prev = np.asarray(prev_luma, dtype=np.uint8)
    cur = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur.shape
    gh = (H + s - 1) // s
    gw = (W + s - 1) // s

    cy0_v = max(0, dy)
    cy1_v = min(H, H + dy)
    cx0_v = max(0, dx)
    cx1_v = min(W, W + dx)

    diff = np.ones((H, W), dtype=bool)

    if cy0_v < cy1_v and cx0_v < cx1_v:
        py0_v = cy0_v - dy
        py1_v = cy1_v - dy
        px0_v = cx0_v - dx
        px1_v = cx1_v - dx

        cur_overlap = cur[cy0_v:cy1_v, cx0_v:cx1_v]
        prev_overlap = prev[py0_v:py1_v, px0_v:px1_v]

        diff[cy0_v:cy1_v, cx0_v:cx1_v] = (cur_overlap != prev_overlap)

    pad_h = gh * s - H
    pad_w = gw * s - W

    if pad_h > 0 or pad_w > 0:
        diff_padded = np.pad(diff, ((0, pad_h), (0, pad_w)), mode='constant', constant_values=False)
    else:
        diff_padded = diff

    diff_reshaped = diff_padded.reshape(gh, s, gw, s).swapaxes(1, 2)
    has_mismatch = np.any(diff_reshaped, axis=(2, 3))

    changed = (cur != prev)
    if pad_h > 0 or pad_w > 0:
        changed = np.pad(changed, ((0, pad_h), (0, pad_w)), mode='constant', constant_values=False)
    changed_tiles = changed.reshape(gh, s, gw, s).swapaxes(1, 2).any(axis=(2, 3))

    return ~has_mismatch & changed_tiles


class TestConstellationVoterPrototype(unittest.TestCase):

    def test_prototype_functions_no_floats(self):
        """Scan prototype functions for float literals, true division and float calls using Python AST."""
        funcs = [estimate_global_motion, consensus_tiles]
        source_code = inspect.getsource(sys_module_of_funcs())
        tree = ast.parse(source_code)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("estimate_global_motion", "consensus_tiles"):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, float):
                        self.fail(f"Float constant {sub.value} found in {node.name}")
                    if isinstance(sub, ast.Div):
                        self.fail(f"True division '/' found in {node.name}")
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "float":
                        self.fail(f"float() call found in {node.name}")

    def test_adv_gm_chrome_clock_and_two_motions(self):
        """Test all chrome_clock and two_motions cases from adv_gm.cases."""
        all_cases = adv_gm.cases(seed=0x5EED, include_1080p=False)

        # Test chrome_clock cases
        clock_cases = [c for c in all_cases if c["kind"] == "chrome_clock"]
        self.assertTrue(len(clock_cases) > 0)
        for c in clock_cases:
            f0, f1 = c["frames"][0], c["frames"][1]
            max_d = max(c["w"], c["h"]) - 1
            res = estimate_global_motion(f0, f1, max_d)
            self.assertIsNotNone(res, f"Failed for case {c['case_id']}")
            self.assertEqual((res[0], res[1]), (0, -24), f"Expected (0, -24) for case {c['case_id']}")

        # Test two_motions cases
        two_cases = [c for c in all_cases if c["kind"] == "two_motions"]
        self.assertTrue(len(two_cases) > 0)
        for c in two_cases:
            f0, f1 = c["frames"][0], c["frames"][1]
            max_d = max(c["w"], c["h"]) - 1
            res = estimate_global_motion(f0, f1, max_d)
            self.assertIsNone(res, f"Expected None for two_motions case {c['case_id']}")

    def test_synthetic_two_blocks(self):
        """Test default_rng(912) 100x150 vs 100x80 blocks on luma-40 (480, 640), requiring None."""
        rng = np.random.default_rng(912)
        f0 = np.full((480, 640), 40, dtype=np.uint8)
        f1 = np.full((480, 640), 40, dtype=np.uint8)

        t1 = rng.integers(0, 256, size=(100, 150), dtype=np.uint8)
        t2 = rng.integers(0, 256, size=(100, 80), dtype=np.uint8)

        f0[64:164, 64:214] = t1
        f1[64:164, 76:226] = t1  # dx=12, dy=0

        f0[280:380, 380:460] = t2
        f1[270:370, 380:460] = t2  # dx=0, dy=-10

        res = estimate_global_motion(f0, f1, max_d=639)
        self.assertIsNone(res, "Expected None for competing two-block motion")

    def test_synthetic_single_block_sequence(self):
        """Test default_rng(101) 96x128 block on luma-250 (480,640) moving by (0,-24) and (0,-4), requiring exact score-zero."""
        rng = np.random.default_rng(101)
        s0 = np.full((480, 640), 250, dtype=np.uint8)
        s1 = np.full((480, 640), 250, dtype=np.uint8)
        s2 = np.full((480, 640), 250, dtype=np.uint8)

        tb = rng.integers(0, 256, size=(96, 128), dtype=np.uint8)
        s0[180:276, 256:384] = tb
        s1[156:252, 256:384] = tb  # dy = -24, dx = 0
        s2[152:248, 256:384] = tb  # dy = -4 relative to s1

        res1 = estimate_global_motion(s0, s1, max_d=639)
        self.assertIsNotNone(res1)
        self.assertEqual(res1, (0, -24, 0))

        res2 = estimate_global_motion(s1, s2, max_d=639)
        self.assertIsNotNone(res2)
        self.assertEqual(res2, (0, -4, 0))

    def test_adv_gm_score_compliance(self):
        """Run adv_gm.score with a test-local module wrapper and check zero wrong_vectors and zero static_relabels."""
        mod = types.ModuleType("gm_constellation")
        mod.estimate_global_motion = estimate_global_motion
        mod.consensus_tiles = consensus_tiles

        res = adv_gm.score(mod, seed=0x5EED, include_1080p=False)
        self.assertEqual(res["status"], "OK")
        tot = res["total"]
        self.assertEqual(tot["wrong_vectors"], 0)
        self.assertEqual(tot["static_relabels"], 0)

    def test_independent_pixel_oracle_consensus(self):
        """Check exact consensus with an independent pixel oracle across synthetic displacements."""
        rng = np.random.default_rng(202)
        H, W, s = 64, 64, 16
        f0 = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        # Shifted f1 by (dx=4, dy=8)
        f1 = np.full((H, W), 128, dtype=np.uint8)
        f1[8:H, 4:W] = f0[0:H - 8, 0:W - 4]

        dx, dy = 4, 8
        ct = consensus_tiles(f0, f1, s, dx, dy)

        # Independent pixel oracle calculation
        gh = (H + s - 1) // s
        gw = (W + s - 1) // s
        expected = np.zeros((gh, gw), dtype=bool)

        for gy in range(gh):
            for gx in range(gw):
                y0, y1 = gy * s, min((gy + 1) * s, H)
                x0, x1 = gx * s, min((gx + 1) * s, W)

                tile_cur = f1[y0:y1, x0:x1]
                tile_prev = f0[y0:y1, x0:x1]

                if np.array_equal(tile_cur, tile_prev):
                    continue

                # Check if every pixel matches translated prev
                match = True
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        py = y - dy
                        px = x - dx
                        if 0 <= py < H and 0 <= px < W:
                            if f1[y, x] != f0[py, px]:
                                match = False
                                break
                        else:
                            match = False
                            break
                    if not match:
                        break

                expected[gy, gx] = match

        np.testing.assert_array_equal(ct, expected)


def sys_module_of_funcs():
    import sys
    return sys.modules[__name__]


if __name__ == "__main__":
    unittest.main()
