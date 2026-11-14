"""Adversarial Test B repeated-glyph collision fixtures and constellation voter witness."""

import unittest
import numpy as np

from eval.v2x import sf_tiledesc as sf
from visioncore.desc import describe
from visioncore.grid import Grid


def make_l_junction_tile(size=16):
    """Create a 16x16 uint8 RGB L-junction tile pattern."""
    tile = np.full((size, size, 3), 220, dtype=np.uint8)
    tile[:4, :, :] = 40
    tile[:, :4, :] = 40
    return tile


def make_repeated_glyph_frame(tile_size=16, grid_cols=8, grid_rows=8):
    """Tile the 16x16 L-junction pattern into an 8x8 grid (128x128 uint8 RGB)."""
    tile = make_l_junction_tile(tile_size)
    return np.tile(tile, (grid_rows, grid_cols, 1))


def independent_phase_hash_oracle(frame):
    """Independent set oracle computing exact phase-covered tile hashes excluding edge tiles."""
    height, width = frame.shape[:2]
    grid = Grid(width, height)
    tiles, _ = describe(frame, grid)
    bags = [tiles["xh64"].copy()]
    shifted = np.empty_like(frame)
    for dx in range(1, min(grid.s, width)):
        shifted[:, :width - dx] = frame[:, dx:]
        shifted[:, width - dx:] = frame[:, -1:]
        current, _ = describe(shifted, grid)
        valid = (grid.tx + 1) * grid.s <= width - dx
        bags.append(current["xh64"][valid])
    for dy in range(1, min(grid.s, height)):
        shifted[:height - dy] = frame[dy:]
        shifted[height - dy:] = frame[-1:]
        current, _ = describe(shifted, grid)
        valid = (grid.ty + 1) * grid.s <= height - dy
        bags.append(current["xh64"][valid])
    return np.unique(np.concatenate(bags)).astype("<u8", copy=False)


class ConstellationVoteWitness:
    """Test-local witness for constellation partner voting policy."""

    def __init__(self, partner_cap=8, vote_min=8):
        self.partner_cap = partner_cap
        self.vote_min = vote_min

    def compute_raw_pairs(self, num_colliding_anchors):
        return num_colliding_anchors * (num_colliding_anchors - 1) // 2

    def vote_anchor(self, matches):
        """
        Group matches by (template_id, template_anchor_id) rather than scene occurrence.
        Cap distinct partner groups at partner_cap and evaluate against vote_min.
        """
        groups = set()
        for m in matches:
            groups.add((m["template_id"], m["template_anchor_id"]))
        raw_group_count = len(groups)
        capped_vote = min(raw_group_count, self.partner_cap)
        passes_min = capped_vote >= self.vote_min
        return {
            "distinct_groups": raw_group_count,
            "capped_vote": capped_vote,
            "passes_min": passes_min,
        }


class TestConstellationCollisionGridB(unittest.TestCase):

    def test_grid_tile_geometry_and_unshifted_descriptors(self):
        frame = make_repeated_glyph_frame(16, 8, 8)
        self.assertEqual(frame.shape, (128, 128, 3))
        self.assertEqual(frame.dtype, np.uint8)

        grid = Grid(frame.shape[1], frame.shape[0])
        self.assertEqual(grid.s, 16)
        self.assertEqual(grid.gw, 8)
        self.assertEqual(grid.gh, 8)
        self.assertEqual(grid.N, 64)

        before = frame.copy()
        tiles, _ = describe(frame, grid)
        self.assertEqual(len(tiles), 64)
        np.testing.assert_array_equal(frame, before)

        # Assert unshifted xh64 and sig64 are constant across all 64 tiles
        first_xh = tiles["xh64"][0]
        first_sig = tiles["sig64"][0]
        self.assertTrue(np.all(tiles["xh64"] == first_xh))
        self.assertTrue(np.all(tiles["sig64"] == first_sig))

    def test_live_fingerprint_unpack_and_deduplication(self):
        frame = make_repeated_glyph_frame(16, 8, 8)
        before = frame.copy()
        fp = sf.screen_fingerprint(frame)
        np.testing.assert_array_equal(frame, before)

        shape, layout, hashes = sf._unpack(fp)
        self.assertEqual(shape, (128, 128, 16))

        # Hash bag deduplication assertion
        unique_hashes = np.unique(hashes)
        self.assertEqual(len(hashes), len(unique_hashes))
        np.testing.assert_array_equal(hashes, unique_hashes)

    def test_independent_phase_hash_oracle_match(self):
        frame = make_repeated_glyph_frame(16, 8, 8)
        fp = sf.screen_fingerprint(frame)
        _, _, hashes = sf._unpack(fp)

        oracle_hashes = independent_phase_hash_oracle(frame)
        np.testing.assert_array_equal(hashes, oracle_hashes)

    def test_constellation_vote_witness_reference_policy(self):
        witness = ConstellationVoteWitness(partner_cap=8, vote_min=8)

        # 1. Raw unbounded pairs for 64 colliding anchors equals 2016
        raw_pairs = witness.compute_raw_pairs(64)
        self.assertEqual(raw_pairs, 2016)
        self.assertEqual(64 * 63 // 2, 2016)

        # 2. Grouping by (template_id, template_anchor_id) prevents repeated template anchor accumulation
        repeated_single_anchor_matches = [
            {"scene_id": s, "template_id": 0, "template_anchor_id": 0}
            for s in range(10)
        ]
        res_single = witness.vote_anchor(repeated_single_anchor_matches)
        self.assertEqual(res_single["distinct_groups"], 1)
        self.assertEqual(res_single["capped_vote"], 1)
        self.assertFalse(res_single["passes_min"])

        # 3. Distinct-template-anchor control increases capped vote
        distinct_anchor_matches = [
            {"scene_id": 0, "template_id": 0, "template_anchor_id": a}
            for a in range(8)
        ]
        res_distinct = witness.vote_anchor(distinct_anchor_matches)
        self.assertEqual(res_distinct["distinct_groups"], 8)
        self.assertEqual(res_distinct["capped_vote"], 8)
        self.assertTrue(res_distinct["passes_min"])

        # Cap limit check: 12 distinct template anchors -> capped at 8
        over_cap_matches = [
            {"scene_id": 0, "template_id": 0, "template_anchor_id": a}
            for a in range(12)
        ]
        res_over = witness.vote_anchor(over_cap_matches)
        self.assertEqual(res_over["distinct_groups"], 12)
        self.assertEqual(res_over["capped_vote"], 8)
        self.assertTrue(res_over["passes_min"])

        # 4. Permute scene/input order to pin determinism
        rng = np.random.RandomState(42)
        permuted_matches = list(distinct_anchor_matches + repeated_single_anchor_matches)
        for _ in range(5):
            rng.shuffle(permuted_matches)
            res_permuted = witness.vote_anchor(permuted_matches)
            self.assertEqual(res_permuted["distinct_groups"], 8)
            self.assertEqual(res_permuted["capped_vote"], 8)
            self.assertTrue(res_permuted["passes_min"])


if __name__ == "__main__":
    unittest.main()
