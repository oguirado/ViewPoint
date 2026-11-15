"""Oracle test suite for visioncore.change _tile_sad_windows, _tile_sad and _tile_sad_clip.

Validates the reference SAD calculation against an independent naive per-pixel loop oracle
and pins reference output digests across 6 fixed seeds. Also supports optional native Spark port
testing via VQA_K_S2_SADWIN_BIN environment variable.
"""
import hashlib
import json
import os
import subprocess
import unittest

import numpy as np

from visioncore import change

# Fixed seed reference SHA-256 digests
GOLDEN_DIGESTS = {
    101: "9fd3d31aff6b231eca0759f49074ecff10dbf534cf7aeefb2459d20b3962e570",
    102: "245cbfb57045599dbd26b600530b804975764befb37545f9ea94491f2cd5cba3",
    103: "0394fee9739e7c08a92424f5d52e8ae91ba22597c08ebe64e4fdaff8d3732269",
    104: "c1db3aa8922cb747dad81f79f5d7725df69ff58f2b51f46a21b1109e9fc1e9f1",
    105: "d5dfc6d6f2b1734950aa8152ad0969116a6850d6283c716a76985cb84e39e494",
    106: "3656dffe6c35e26e2ea9195790b13079377d85a33b95caec785c578a8939bbb5",
}


def naive_sad_windows(y_cur, y_prev, c, s, tx, ty):
    """Naive per-pixel loop over requested tiles.

    Sums absolute differences where shifted source is inside the frame and counts those pixels.
    """
    dx, dy = c
    h, w = y_cur.shape
    tx = np.asarray(tx, dtype=np.int64)
    ty = np.asarray(ty, dtype=np.int64)
    tx_b, ty_b = np.broadcast_arrays(tx, ty)
    shape = tx_b.shape
    tx_flat = tx_b.ravel()
    ty_flat = ty_b.ravel()

    # Negative tile index normalization matching NumPy/visioncore indexing
    tx_norm = np.where(tx_flat < 0, tx_flat + w // s, tx_flat)
    ty_norm = np.where(ty_flat < 0, ty_flat + h // s, ty_flat)

    sads = []
    counts = []

    for t_x, t_y in zip(tx_norm, ty_norm):
        yy = t_y * s
        xx = t_x * s
        sad_i = 0
        cnt_i = 0
        for r in range(s):
            yc = yy + r
            yp = yc - dy
            for col in range(s):
                xc = xx + col
                xp = xc - dx
                if 0 <= yc < h and 0 <= xc < w and 0 <= yp < h and 0 <= xp < w:
                    sad_i += abs(int(y_cur[yc, xc]) - int(y_prev[yp, xp]))
                    cnt_i += 1
        sads.append(sad_i)
        counts.append(cnt_i)

    if len(sads) == 0:
        sad_arr = np.zeros(shape, dtype=np.int64)
        cnt_arr = np.zeros(shape, dtype=np.int64)
    else:
        sad_arr = np.array(sads, dtype=np.int64).reshape(shape)[()] if shape else np.int64(sads[0])
        cnt_arr = np.array(counts, dtype=np.int64).reshape(shape)[()] if shape else np.int64(counts[0])
    return sad_arr, cnt_arr


class TestKrefS2Sadwin(unittest.TestCase):

    def test_sadwin_naive_oracle_comparison(self):
        """Compare visioncore.change SAD helpers against naive per-pixel loop oracle on RAM fixtures."""
        seeds = [1, 2, 3]
        tile_sizes = [16, 32]

        for seed in seeds:
            rng = np.random.default_rng(seed)
            for s in tile_sizes:
                # Frame sizes: multiple of s and NOT multiple of s
                frame_dims = [(s * 3, s * 4), (s * 3 + 5, s * 4 + 7)]
                for h, w in frame_dims:
                    gh, gw = h // s, w // s
                    for fixture_kind in ("random_int16", "extremes_int16"):
                        if fixture_kind == "random_int16":
                            y_cur = rng.integers(-32768, 32768, (h, w), dtype=np.int16)
                            y_prev = rng.integers(-32768, 32768, (h, w), dtype=np.int16)
                        else:
                            y_cur = rng.choice(np.array([-32768, 32767], np.int16), (h, w))
                            y_prev = rng.choice(np.array([-32768, 32767], np.int16), (h, w))

                        # Requested vectors: 0, +-1, s-1, s, 2s, beyond frame
                        vectors = [
                            (0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1),
                            (s - 1, 0), (0, s - 1), (s, 0), (0, s), (2 * s, 0), (-2 * s, 0),
                            (w + 10, h + 10), (-w - 10, -h - 10)
                        ]

                        ty_grid, tx_grid = np.meshgrid(np.arange(gh), np.arange(gw), indexing="ij")
                        tx_all, ty_all = tx_grid.ravel(), ty_grid.ravel()

                        # Border tile request, all tiles, and empty request
                        border_mask = (tx_all == 0) | (tx_all == gw - 1) | (ty_all == 0) | (ty_all == gh - 1)
                        tx_border, ty_border = tx_all[border_mask], ty_all[border_mask]

                        requests = [
                            (tx_all, ty_all),
                            (tx_border, ty_border),
                            (np.array([], int), np.array([], int)),
                        ]

                        for vec in vectors:
                            for tx, ty in requests:
                                # Test _tile_sad_windows
                                ref_sad, ref_cnt = change._tile_sad_windows(y_cur, y_prev, vec, s, tx, ty)
                                oracle_sad, oracle_cnt = naive_sad_windows(y_cur, y_prev, vec, s, tx, ty)

                                np.testing.assert_array_equal(ref_sad, oracle_sad)
                                np.testing.assert_array_equal(ref_cnt, oracle_cnt)

                                # Test _tile_sad
                                sad_out, valid_out = change._tile_sad(y_cur, y_prev, vec, s, gh, gw, tx, ty)
                                np.testing.assert_array_equal(sad_out, oracle_sad)
                                np.testing.assert_array_equal(valid_out, oracle_cnt == (s * s))

                                # Test _tile_sad_clip
                                clip_sad, clip_cnt = change._tile_sad_clip(y_cur, y_prev, vec, s, gh, gw, tx, ty)
                                np.testing.assert_array_equal(clip_sad, oracle_sad)
                                np.testing.assert_array_equal(clip_cnt, oracle_cnt)

    def test_pinned_seed_digests(self):
        """Verify output digests for 6 fixed seeds match pinned golden values."""
        for seed, expected_digest in GOLDEN_DIGESTS.items():
            rng = np.random.default_rng(seed)
            s = 16
            h, w = 48, 64
            gh, gw = h // s, w // s
            cur = rng.integers(-32768, 32768, (h, w), dtype=np.int16)
            prev = rng.integers(-32768, 32768, (h, w), dtype=np.int16)
            ty_grid, tx_grid = np.meshgrid(np.arange(gh), np.arange(gw), indexing="ij")
            tx, ty = tx_grid.ravel(), ty_grid.ravel()
            vec = (3, -2)

            sad, count = change._tile_sad_windows(cur, prev, vec, s, tx, ty)
            data = sad.tobytes() + count.tobytes()
            digest = hashlib.sha256(data).hexdigest()
            self.assertEqual(digest, expected_digest, f"Digest mismatch for seed {seed}")

    def test_native_spark_port_differential(self):
        """Differential test against paired Spark release binary via VQA_K_S2_SADWIN_BIN."""
        bin_path = os.environ.get("VQA_K_S2_SADWIN_BIN")
        if not bin_path or not os.path.exists(bin_path):
            self.skipTest("VQA_K_S2_SADWIN_BIN environment variable not set or binary not found")

        rng = np.random.default_rng(2024)
        s = 16
        h, w = 32, 48
        gh, gw = h // s, w // s
        cur = rng.integers(-32768, 32768, (h, w), dtype=np.int16)
        prev = rng.integers(-32768, 32768, (h, w), dtype=np.int16)
        tx, ty = np.array([0, 1]), np.array([0, 1])
        vec = (2, -1)

        ref_sad, ref_cnt = change._tile_sad_windows(cur, prev, vec, s, tx, ty)

        input_payload = json.dumps({
            "s": s, "h": h, "w": w, "vec": vec,
            "tx": tx.tolist(), "ty": ty.tolist(),
            "y_cur": cur.tolist(), "y_prev": prev.tolist()
        })

        proc = subprocess.run([bin_path], input=input_payload.encode(), capture_output=True, check=True)
        resp = json.loads(proc.stdout.decode())

        np.testing.assert_array_equal(np.array(resp["sad"], dtype=np.int64), ref_sad)
        np.testing.assert_array_equal(np.array(resp["count"], dtype=np.int64), ref_cnt)


if __name__ == "__main__":
    unittest.main()
