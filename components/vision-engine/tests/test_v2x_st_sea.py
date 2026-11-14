"""Tests for eval/v2x/st_sea.py sub-tile motion variant comparing against st_block4 baseline."""

import unittest
import numpy as np
import eval.v2x.st_block4 as st_block4
import eval.v2x.st_sea as st_sea


class TestV2XStSea(unittest.TestCase):

    def setUp(self):
        st_block4.reset_ops()
        st_sea.reset_ops()

    def test_ops_counter_and_reset(self):
        """Test reset_ops and OPS counter behavior."""
        st_sea.reset_ops()
        self.assertEqual(st_sea.OPS, 0)

        W, H, s = 128, 128, 32
        f1 = np.full((H, W), 200, dtype=np.uint8)
        f2 = np.full((H, W), 200, dtype=np.uint8)
        f1[40:56, 40:42] = 50
        f2[40:56, 52:54] = 50

        st_sea.subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=16)
        self.assertGreater(st_sea.OPS, 0)

        st_sea.reset_ops()
        self.assertEqual(st_sea.OPS, 0)

    def test_300_random_cases(self):
        """Compare st_sea and st_block4 outputs and OPS on 300 seeded RAM cases."""
        rng = np.random.RandomState(42)

        # Pre-define fixed scene configs
        configs = []

        # 1. Frame border & edge cases with various tile sizes, frame sizes, and max_d (1..15)
        for i in range(100):
            s = rng.choice([16, 32, 64])
            gw = rng.randint(2, 6)
            gh = rng.randint(2, 6)
            # Edge dimensions (sometimes non-multiple of s or 8)
            pad_w = rng.randint(0, 7)
            pad_h = rng.randint(0, 7)
            W = gw * s + pad_w
            H = gh * s + pad_h
            tx = rng.choice([0, gw - 1, rng.randint(0, gw)])
            ty = rng.choice([0, gh - 1, rng.randint(0, gh)])
            max_d = (i % 15) + 1  # 1 to 15
            configs.append((W, H, s, (tx, ty), max_d, "border"))

        # 2. Flat regions and low contrast cases
        for i in range(100):
            s = rng.choice([16, 32, 64])
            gw, gh = 4, 4
            W, H = gw * s, gh * s
            tx, ty = rng.randint(0, gw), rng.randint(0, gh)
            max_d = (i % 15) + 1  # 1 to 15
            configs.append((W, H, s, (tx, ty), max_d, "flat"))

        # 3. Repeated textures, checkerboards, stripes, and active motions
        for i in range(100):
            s = rng.choice([16, 32, 64])
            gw, gh = 4, 4
            W, H = gw * s, gh * s
            tx, ty = rng.randint(0, gw), rng.randint(0, gh)
            max_d = (i % 15) + 1  # 1 to 15
            configs.append((W, H, s, (tx, ty), max_d, "texture"))

        self.assertEqual(len(configs), 300)

        for case_idx, (W, H, s, tile_xy, max_d, case_type) in enumerate(configs):
            f1 = rng.randint(0, 256, (H, W), dtype=np.uint8)

            if case_type == "flat":
                # Create large flat regions
                f1[:, :] = rng.randint(0, 256)
                if rng.rand() < 0.5:
                    # Add small feature
                    bx = rng.randint(0, W - 4)
                    by = rng.randint(0, H - 4)
                    f1[by:by + 4, bx:bx + 4] = rng.randint(0, 256)
                f2 = f1.copy()
                if rng.rand() < 0.5:
                    # Shift region
                    dx = rng.randint(-max_d, max_d + 1)
                    dy = rng.randint(-max_d, max_d + 1)
                    f2 = np.roll(np.roll(f1, dy, axis=0), dx, axis=1)

            elif case_type == "texture":
                # Create repeated textures (stripes / checkerboards)
                pat_type = rng.choice(["stripes", "checker", "random_blocks"])
                if pat_type == "stripes":
                    f1 = np.tile(rng.randint(0, 256, (1, 8), dtype=np.uint8), (H, (W + 7) // 8))[:H, :W]
                elif pat_type == "checker":
                    chk = np.array([[50, 200], [200, 50]], dtype=np.uint8)
                    f1 = np.tile(chk, ((H + 1) // 2, (W + 1) // 2))[:H, :W]
                else:
                    blk = rng.randint(0, 256, (4, 4), dtype=np.uint8)
                    f1 = np.tile(blk, ((H + 3) // 4, (W + 3) // 4))[:H, :W]

                f2 = f1.copy()
                if rng.rand() < 0.8:
                    dx = rng.randint(-max_d, max_d + 1)
                    dy = rng.randint(-max_d, max_d + 1)
                    # Translate f1 to f2 with boundary handling
                    f2 = np.full_like(f1, rng.randint(0, 256))
                    sy0, sy1 = max(0, -dy), min(H, H - dy)
                    sx0, sx1 = max(0, -dx), min(W, W - dx)
                    dy0, dy1 = max(0, dy), min(H, H + dy)
                    dx0, dx1 = max(0, dx), min(W, W + dx)
                    if sy1 > sy0 and sx1 > sx0:
                        f2[dy0:dy1, dx0:dx1] = f1[sy0:sy1, sx0:sx1]

            else:  # border / random noise
                f2 = f1.copy()
                if rng.rand() < 0.7:
                    dx = rng.randint(-max_d, max_d + 1)
                    dy = rng.randint(-max_d, max_d + 1)
                    # Local motion box in 3x3 neighbor union
                    tx, ty = tile_xy
                    gw_g = (W + s - 1) // s
                    gh_g = (H + s - 1) // s
                    tx0 = max(0, tx - 1) * s
                    tx1 = min(W, min(gw_g, tx + 2) * s)
                    ty0 = max(0, ty - 1) * s
                    ty1 = min(H, min(gh_g, ty + 2) * s)
                    if tx1 > tx0 and ty1 > ty0:
                        sub = f1[ty0:ty1, tx0:tx1].copy()
                        f2[ty0:ty1, tx0:tx1] = rng.randint(0, 256)
                        s_y0, s_y1 = max(ty0, ty0 + dy), min(ty1, ty1 + dy)
                        s_x0, s_x1 = max(tx0, tx0 + dx), min(tx1, tx1 + dx)
                        if s_y1 > s_y0 and s_x1 > s_x0:
                            f2[s_y0:s_y1, s_x0:s_x1] = sub[s_y0 - ty0 - dy:s_y1 - ty0 - dy, s_x0 - tx0 - dx:s_x1 - tx0 - dx]

            st_block4.reset_ops()
            res_b4 = st_block4.subtile_motion(f1, f2, s=s, tile_xy=tile_xy, max_d=max_d)
            ops_b4 = st_block4.OPS

            st_sea.reset_ops()
            res_sea = st_sea.subtile_motion(f1, f2, s=s, tile_xy=tile_xy, max_d=max_d)
            ops_sea = st_sea.OPS

            self.assertEqual(res_sea, res_b4, f"Mismatch on case {case_idx}: st_sea={res_sea}, st_block4={res_b4}")
            self.assertLessEqual(ops_sea, ops_b4, f"OPS higher on case {case_idx}: st_sea={ops_sea}, st_block4={ops_b4}")


if __name__ == "__main__":
    unittest.main()
