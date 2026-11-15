import unittest
import numpy as np

from eval.v2x.gm_bands_hv import estimate_global_motion, consensus_tiles
from eval.v2eval.postpass import PostPass
from visioncore.desc import luma_of_padded, describe
from visioncore.grid import Grid


class TestIdenticalAndStaticChromeZeroVector(unittest.TestCase):
    def test_identical_and_static_chrome_zero_vector(self):
        # Generate RAM uint8 RGB frame (128, 160, 3) using default_rng(1010)
        rng = np.random.default_rng(1010)
        rgb = rng.integers(0, 256, (128, 160, 3), dtype=np.uint8)

        # Use luma_of_padded to obtain non-flat luma
        luma = luma_of_padded(rgb).astype(np.uint8)

        # Require estimate_global_motion on identical non-flat luma at max_d=100 to return (0, 0, 0)
        vec = estimate_global_motion(luma, luma, max_d=100)
        self.assertEqual(vec, (0, 0, 0))

        # Require consensus_tiles at s=16 and zero displacement to be boolean and entirely false
        ct = consensus_tiles(luma, luma, s=16, dx=0, dy=0)
        self.assertEqual(ct.dtype, bool)
        self.assertFalse(bool(ct.any()))

        # Construct real grid and baseline tile descriptors using repo API
        grid = Grid(160, 128)
        desc1, _ = describe(rgb, grid)

        # Feed the same RGB frame twice to PostPass update
        postpass = PostPass(grid)
        out1, _ = postpass.update(desc1, rgb)
        out2, _ = postpass.update(desc1, rgb)

        # Compare baseline and postpass descriptors; require no new MOVED claims or nonzero motion vectors
        base_cls = out2["baseline"]["cls"]
        for variant_name, variant_out in out2.items():
            np.testing.assert_array_equal(
                variant_out["mv_dx"],
                0,
                err_msg=f"Nonzero mv_dx found in variant {variant_name}",
            )
            np.testing.assert_array_equal(
                variant_out["mv_dy"],
                0,
                err_msg=f"Nonzero mv_dy found in variant {variant_name}",
            )
            np.testing.assert_array_equal(
                variant_out["cls"],
                base_cls,
                err_msg=f"Class difference/MOVED claims found in variant {variant_name}",
            )


if __name__ == "__main__":
    unittest.main()
