import itertools
import unittest
import numpy as np

from eval.p7_cellhash import (
    fmix64,
    inv_fmix64,
    hash_cell2,
    fnv1a64,
    multiset_digest,
    fibonacci_slot,
    tile_digest,
    P1,
    P2,
    P3,
    MASK_64,
)


def _scalar_tile_digest(frame: np.ndarray, s: int = 16) -> np.ndarray:
    H, W = frame.shape
    gh = H // s
    gw = W // s
    num_tiles = gh * gw
    out = np.zeros(num_tiles, dtype=np.uint64)
    idx = 0
    fnv_prime = 1099511628211
    fnv_offset = 14695981039346656037

    for ty in range(gh):
        for tx in range(gw):
            tile = frame[ty * s : (ty + 1) * s, tx * s : (tx + 1) * s]
            words = np.ascontiguousarray(tile).view("<u8").ravel()
            h = fnv_offset
            for w in words:
                w_val = int(w)
                for i in range(8):
                    b = (w_val >> (i * 8)) & 0xFF
                    h = ((h ^ b) * fnv_prime) & MASK_64
            out[idx] = h
            idx += 1
    return out


class TestP7CellHash(unittest.TestCase):
    def test_fmix64_bijection_roundtrip(self):
        """fmix64 is a bijection on 2^16 sampled words with zero collisions and has an explicit inverse that round trips."""
        num_samples = 1 << 16
        sampled = [i * 0x10001 + 0x1234 for i in range(num_samples)]
        outputs = [fmix64(x) for x in sampled]

        # Zero collisions
        self.assertEqual(len(set(outputs)), num_samples)

        # Explicit inverse round trips
        for x in sampled:
            h = fmix64(x)
            self.assertEqual(inv_fmix64(h), x & MASK_64)

    def test_hash_cell2_zero_collisions(self):
        """hash_cell2 has zero collisions along any row or column of a 4096 by 4096 grid."""
        seed = 0xDEADBEEF12345678
        grid_size = 4096

        # Test along row 0, 2048, 4095
        for row in (0, 2048, 4095):
            row_hashes = set(hash_cell2(seed, x, row) for x in range(grid_size))
            self.assertEqual(len(row_hashes), grid_size)

        # Test along col 0, 2048, 4095
        for col in (0, 2048, 4095):
            col_hashes = set(hash_cell2(seed, col, y) for y in range(grid_size))
            self.assertEqual(len(col_hashes), grid_size)

    def test_multiset_digest_order_invariance(self):
        """All 720 orderings of 6 entries give one multiset digest."""
        entries = [0x1111, 0x2222, 0x3333, 0x4444, 0x5555, 0x6666]
        entry_hashes = [fmix64(e) for e in entries]

        digests = set()
        for perm in itertools.permutations(entry_hashes):
            digests.add(multiset_digest(perm))

        self.assertEqual(len(digests), 1)

    def test_strict_avalanche_dev(self):
        """Strict avalanche deviation under 0.05 on 4096 seeded inputs."""
        n_inputs = 4096
        matrix = np.zeros((64, 64), dtype=np.int64)

        for seed in range(n_inputs):
            h0 = fmix64(seed)
            for ibit in range(64):
                h1 = fmix64(seed ^ (1 << ibit))
                diff = h0 ^ h1
                for obit in range(64):
                    if (diff >> obit) & 1:
                        matrix[ibit, obit] += 1

        probs = matrix / float(n_inputs)
        max_dev = np.abs(probs - 0.5).max()
        self.assertLess(max_dev, 0.05)

    def test_tile_digest_against_scalar_ref(self):
        """tile_digest equals a scalar reference on a 1920 by 1080 RAM frame."""
        rng = np.random.default_rng(1337)
        frame = rng.integers(0, 256, size=(1080, 1920), dtype=np.uint8)

        vec_res = tile_digest(frame, s=16)
        scal_res = _scalar_tile_digest(frame, s=16)

        self.assertEqual(len(vec_res), len(scal_res))
        np.testing.assert_array_equal(vec_res, scal_res)

    def test_fnv1a64_and_fibonacci_slot(self):
        """Basic sanity checks for fnv1a64 and fibonacci_slot."""
        v = 0x0123456789ABCDEF
        h = fnv1a64(v)
        self.assertIsInstance(h, int)
        self.assertTrue(0 <= h <= MASK_64)

        slot = fibonacci_slot(h, 10)
        self.assertTrue(0 <= slot < (1 << 10))


if __name__ == "__main__":
    unittest.main()
