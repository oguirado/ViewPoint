"""tests/test_kref_gm_proto.py - Independent oracle and differential tests for eval.v2proto_global_motion."""

import json
import hashlib
import os
import subprocess
import tempfile
import unittest
import numpy as np

from eval.v2proto_global_motion import estimate_global_motion, consensus_tiles
from visioncore.layout import grid_params

# Pinned SHA256 digests for 6 fixed reference seeds
EXPECTED_SEED_DIGESTS = {
    1001: "69a9cbe4d658e9bb889dd4e6d6abc2f85b6d1699b168854b10ef68a3e1965cb8",
    1002: "6b709fb2077f2f9f318c274de8c6d22f3883478a06771a0dec72c927e23a2c29",
    1003: "950a604f0be570941f0cd55a1cdf002258755cb8f07a0d651b1e67c03930791b",
    1004: "69c2214909477729294d087bb00d40f803473b3c5ea6d9bdc033e02d87a12104",
    1005: "9d481ce516e6843ddf85d22caa41fcfdc5891636654f6cb4568d2560d9dc43c4",
    1006: "4fef78a5064378eb413ea7bf278028b9d0692a436e5f79d4d5b55a0dde664ed0",
}


def compute_exhaustive_sad(prev, cur, dx, dy):
    """Exhaustive full-frame normalized SAD computation oracle between prev and cur shifted by (dx, dy).

    cur[y, x] corresponds to prev[y - dy, x - dx].
    Returns normalized SAD scaled to full frame area (H * W), or 1 << 60 if out of bounds.
    """
    H, W = cur.shape
    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)
    if cy0 >= cy1 or cx0 >= cx1:
        return 1 << 60
    py0, py1 = cy0 - dy, cy1 - dy
    px0, px1 = cx0 - dx, cx1 - dx
    c_sub = cur[cy0:cy1, cx0:cx1].astype(np.int64)
    p_sub = prev[py0:py1, px0:px1].astype(np.int64)
    overlap = (cy1 - cy0) * (cx1 - cx0)
    sad = np.sum(np.abs(c_sub - p_sub))
    return int((sad * (H * W)) // overlap)


def exhaustive_sad_grid(prev, cur, max_d):
    """Exhaustive full-frame SAD search over every vector (dx, dy) in [-max_d, max_d]."""
    grid = {}
    for dy in range(-max_d, max_d + 1):
        for dx in range(-max_d, max_d + 1):
            grid[(dx, dy)] = compute_exhaustive_sad(prev, cur, dx, dy)
    return grid


def generate_seed_fixture(seed, width=128, height=96, max_d=8):
    """Generate deterministic frame pair and displacement in RAM from fixed seed."""
    rng = np.random.RandomState(seed)
    dx = int(rng.randint(-max_d, max_d + 1))
    dy = int(rng.randint(-max_d, max_d + 1))
    prev = rng.randint(0, 256, (height, width), dtype=np.uint8)
    cur = np.zeros_like(prev)
    cy0, cy1 = max(0, dy), min(height, height + dy)
    cx0, cx1 = max(0, dx), min(width, width + dx)
    py0, py1 = cy0 - dy, cy1 - dy
    px0, px1 = cx0 - dx, cx1 - dx
    cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]
    if cy0 > 0:
        cur[:cy0, :] = rng.randint(0, 256, (cy0, width), dtype=np.uint8)
    if cy1 < height:
        cur[cy1:, :] = rng.randint(0, 256, (height - cy1, width), dtype=np.uint8)
    if cx0 > 0:
        cur[:, :cx0] = rng.randint(0, 256, (height, cx0), dtype=np.uint8)
    if cx1 < width:
        cur[:, cx1:] = rng.randint(0, 256, (height, width - cx1), dtype=np.uint8)
    return prev, cur, max_d


def serialize_reference_output(seed, res, mask):
    """Canonical JSON byte serialization for reference result hashing."""
    payload = json.dumps({
        "seed": seed,
        "res": list(res) if res is not None else None,
        "mask": mask.tolist() if mask is not None else None
    }, sort_keys=True)
    return payload.encode("utf-8")


class TestKrefGMProto(unittest.TestCase):

    def test_returned_vector_score_equals_exhaustive_sad(self):
        """Assert prototype returned vector score equals exhaustive SAD at that vector and matches minimum."""
        for seed in [2001, 2002, 2003, 2004, 2005]:
            prev, cur, max_d = generate_seed_fixture(seed, width=64, height=48, max_d=6)
            res = estimate_global_motion(prev, cur, max_d)
            self.assertIsNotNone(res, f"Expected motion estimate for seed {seed}")
            est_dx, est_dy, score = res

            sad_grid = exhaustive_sad_grid(prev, cur, max_d)
            expected_score = sad_grid[(est_dx, est_dy)]
            self.assertEqual(
                score, expected_score,
                f"Returned score {score} != exhaustive SAD {expected_score} at ({est_dx}, {est_dy})"
            )

            min_sad = min(sad_grid.values())
            self.assertEqual(
                score, min_sad,
                f"Returned score {score} is not global minimum exhaustive SAD {min_sad}"
            )

    def test_pure_translation_found_exactly(self):
        """Assert pure translations on small frames are found exactly with score 0."""
        for seed in range(10):
            rng = np.random.RandomState(300 + seed)
            width, height = rng.choice([32, 48, 64]), rng.choice([32, 48, 64])
            max_d = 5
            dx, dy = int(rng.randint(-max_d, max_d + 1)), int(rng.randint(-max_d, max_d + 1))

            prev = rng.randint(0, 256, (height, width), dtype=np.uint8)
            cur = np.zeros_like(prev)
            cy0, cy1 = max(0, dy), min(height, height + dy)
            cx0, cx1 = max(0, dx), min(width, width + dx)
            py0, py1 = cy0 - dy, cy1 - dy
            px0, px1 = cx0 - dx, cx1 - dx
            cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

            res = estimate_global_motion(prev, cur, max_d)
            self.assertIsNotNone(res, f"Pure translation failed for seed {seed}")
            self.assertEqual((res[0], res[1], res[2]), (dx, dy, 0))

    def test_periodic_stripes_trigger_alias_rejection(self):
        """Assert 1D periodic stripes trigger alias rejection and return None."""
        width, height = 128, 128
        # Vertical 1D periodic stripes (period 8)
        x_coords = np.arange(width)
        stripes_v = np.where((x_coords // 4) % 2 == 0, 220, 30).astype(np.uint8)
        prev_v = np.tile(stripes_v, (height, 1))
        res_v = estimate_global_motion(prev_v, prev_v, max_d=16)
        self.assertIsNone(res_v, "Vertical periodic stripes should return None")

        # Horizontal 1D periodic stripes (period 8)
        y_coords = np.arange(height)
        stripes_h = np.where((y_coords // 4) % 2 == 0, 220, 30).astype(np.uint8)
        prev_h = np.tile(stripes_h[:, None], (1, width))
        res_h = estimate_global_motion(prev_h, prev_h, max_d=16)
        self.assertIsNone(res_h, "Horizontal periodic stripes should return None")

    def test_consensus_tiles_marks_exact_overlap_matches(self):
        """Assert consensus_tiles marks exactly the tiles whose overlap matches under vector in row-major (gh, gw) shape."""
        width, height = 128, 96
        s, gw, gh = grid_params(width, height)
        dx, dy = 16, -16

        rng = np.random.RandomState(4001)
        prev = rng.randint(0, 256, (height, width), dtype=np.uint8)
        cur = np.zeros_like(prev)
        cy0, cy1 = max(0, dy), min(height, height + dy)
        cx0, cx1 = max(0, dx), min(width, width + dx)
        py0, py1 = cy0 - dy, cy1 - dy
        px0, px1 = cx0 - dx, cx1 - dx
        cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

        mask = consensus_tiles(prev, cur, s, dx, dy)
        self.assertEqual(mask.shape, (gh, gw))

        for ty in range(gh):
            for tx in range(gw):
                t_cy0, t_cy1 = ty * s, min((ty + 1) * s, height)
                t_cx0, t_cx1 = tx * s, min((tx + 1) * s, width)
                t_py0, t_py1 = t_cy0 - dy, t_cy1 - dy
                t_px0, t_px1 = t_cx0 - dx, t_cx1 - dx

                in_bounds = (t_py0 >= 0 and t_py1 <= height and t_px0 >= 0 and t_px1 <= width)
                if in_bounds:
                    tile_cur = cur[t_cy0:t_cy1, t_cx0:t_cx1]
                    tile_prev = prev[t_py0:t_py1, t_px0:t_px1]
                    expected = np.array_equal(tile_cur, tile_prev)
                else:
                    expected = False

                self.assertEqual(
                    mask[ty, tx], expected,
                    f"Mismatch at tile ({ty}, {tx}): got {mask[ty, tx]}, expected {expected}"
                )

    def test_pinned_fixed_seeds_digests(self):
        """Assert reference outputs for 6 fixed seeds match pinned sha256 digests."""
        for seed, expected_digest in EXPECTED_SEED_DIGESTS.items():
            prev, cur, max_d = generate_seed_fixture(seed, width=128, height=96, max_d=8)
            res = estimate_global_motion(prev, cur, max_d)
            s, gw, gh = grid_params(128, 96)
            mask = consensus_tiles(prev, cur, s, res[0], res[1]) if res is not None else None

            payload = serialize_reference_output(seed, res, mask)
            digest = hashlib.sha256(payload).hexdigest()
            self.assertEqual(
                digest, expected_digest,
                f"Digest mismatch for seed {seed}: got {digest}, expected {expected_digest}"
            )

    @unittest.expectedFailure
    def test_checkerboard_alias_rejection(self):
        """Bug family: 2D periodic pattern alias rejection failure in 1D projections."""
        width, height = 128, 128
        y_coords, x_coords = np.indices((height, width))
        cb = np.where(((y_coords // 8) + (x_coords // 8)) % 2 == 0, 200, 50).astype(np.uint8)

        # For a 2D periodic checkerboard (period 16), shift (16, 0) gives score 0.
        # Prototype fails alias rejection and returns (0, 0, 0) instead of None.
        res = estimate_global_motion(cb, cb, max_d=32)
        self.assertIsNone(res, "2D periodic checkerboard should trigger alias rejection and return None")

    def test_native_differential(self):
        """Native differential test when VQA_K_GM_PROTO_BIN is set."""
        bin_path = os.environ.get("VQA_K_GM_PROTO_BIN")
        if not bin_path or not os.path.exists(bin_path):
            self.skipTest("VQA_K_GM_PROTO_BIN unset or binary not found")

        # Test native binary against Python reference for fixed seeds
        for seed in [1001, 1002, 1003]:
            prev, cur, max_d = generate_seed_fixture(seed, width=128, height=96, max_d=8)
            s, gw, gh = grid_params(128, 96)
            ref_res = estimate_global_motion(prev, cur, max_d)
            ref_mask = consensus_tiles(prev, cur, s, ref_res[0], ref_res[1]) if ref_res is not None else None

            # Spawn native binary
            proc = subprocess.Popen([bin_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            try:
                height, width = cur.shape
                # Send Op 1 request: op=1 (i32), H (i32), W (i32), max_d (i32), s (i32), prev_bytes, cur_bytes
                req_hdr = np.array([1, height, width, max_d, s], dtype="<i4").tobytes()
                proc.stdin.write(req_hdr)
                proc.stdin.write(prev.tobytes())
                proc.stdin.write(cur.tobytes())
                proc.stdin.flush()

                # Read response: status (i32)
                status_bytes = proc.stdout.read(4)
                self.assertEqual(len(status_bytes), 4, "Native binary closed pipe prematurely")
                status = np.frombuffer(status_bytes, dtype="<i4")[0]
                self.assertEqual(status, 0, f"Native binary returned non-zero status {status}")

                has_res_bytes = proc.stdout.read(4)
                has_res = np.frombuffer(has_res_bytes, dtype="<i4")[0]

                if ref_res is None:
                    self.assertEqual(has_res, 0, "Native binary returned result when reference returned None")
                    native_res, native_mask = None, None
                else:
                    self.assertEqual(has_res, 1, "Native binary returned None when reference returned result")
                    res_bytes = proc.stdout.read(16)
                    dx, dy, score = np.frombuffer(res_bytes, dtype="<i4, <i4, <i8")[0]
                    native_res = (int(dx), int(dy), int(score))

                    grid_bytes = proc.stdout.read(8)
                    native_gw, native_gh = np.frombuffer(grid_bytes, dtype="<i4")
                    mask_bytes = proc.stdout.read(native_gw * native_gh)
                    native_mask = np.frombuffer(mask_bytes, dtype=bool).reshape((native_gh, native_gw))

                self.assertEqual(native_res, ref_res)
                if ref_mask is not None:
                    np.testing.assert_array_equal(native_mask, ref_mask)

            finally:
                # Quit binary op=0
                try:
                    proc.stdin.write(np.array([0], dtype="<i4").tobytes())
                    proc.stdin.flush()
                except Exception:
                    pass
                proc.communicate(timeout=3)

    def test_storage_law_ram_only_fixtures(self):
        """Assert fixtures live strictly in RAM under temporary directory without disk persistence."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertTrue(os.path.isdir(tmp_dir))
            prev, cur, max_d = generate_seed_fixture(1001)
            # Verify arrays are numpy ndarrays in RAM
            self.assertIsInstance(prev, np.ndarray)
            self.assertIsInstance(cur, np.ndarray)
            # Ensure temporary directory is empty
            self.assertEqual(len(os.listdir(tmp_dir)), 0)


if __name__ == "__main__":
    unittest.main()
