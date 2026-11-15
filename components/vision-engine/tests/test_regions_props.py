"""Property and negative tests for S4 region grouping helpers in visioncore/regions.py."""
import random
import unittest
import numpy as np

from visioncore import grid, layout, regions as R
from visioncore.errors import VqaError


def _bfs_islands(cells):
    """Reference BFS implementation for 8-connected components on a dict of (tx, ty) -> morton."""
    visited = set()
    components = []
    nb8 = tuple((dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx, dy) != (0, 0))
    for cell in sorted(cells.keys()):
        if cell in visited:
            continue
        comp = []
        queue = [cell]
        visited.add(cell)
        while queue:
            curr = queue.pop(0)
            comp.append(curr)
            for dx, dy in nb8:
                nbr = (curr[0] + dx, curr[1] + dy)
                if nbr in cells and nbr not in visited:
                    visited.add(nbr)
                    queue.append(nbr)
        components.append(comp)
    return components


class TestCompactVProps(unittest.TestCase):
    def test_compact_v_seeds_0_to_9(self):
        for seed in range(10):
            rnd = random.Random(seed)
            vals = [rnd.randint(0, 0xFFFF) for _ in range(200)]
            arr = np.array(vals, dtype=np.uint16)
            res = R._compact_v(arr)
            expected = np.array([layout._compact(v) for v in vals], dtype=np.uint16)
            np.testing.assert_array_equal(res, expected)

    def test_compact_v_negative_empty(self):
        empty = np.array([], dtype=np.uint16)
        res = R._compact_v(empty)
        self.assertEqual(len(res), 0)
        np.testing.assert_array_equal(res, empty)


class TestGroupProps(unittest.TestCase):
    def test_group_exhaustive_enums(self):
        # Test every class (0..10) across dx, dy combinations generated with seeds 0..9
        for seed in range(10):
            rnd = random.Random(seed)
            dx, dy = rnd.randint(-100, 100), rnd.randint(-100, 100)
            for cls in range(10):
                res = R._group(cls, dx, dy)
                if cls in (R._C_CHANGED, R._C_FLICKER, R._C_DRIFT):
                    self.assertEqual(res, ("CHG",))
                elif cls == R._C_MOVED:
                    self.assertEqual(res, ("MOV", dx, dy))
                elif cls == R._C_FROZEN:
                    self.assertEqual(res, ("FRZ",))
                else:
                    self.assertIsNone(res)

    def test_group_negative_non_dirty(self):
        self.assertIsNone(R._group(0, 0, 0))  # UNCHANGED
        self.assertIsNone(R._group(1, 10, -10))  # JITTER
        self.assertIsNone(R._group(99, 0, 0))  # Invalid class


class TestKindOfProps(unittest.TestCase):
    def test_kind_of_exhaustive_enums(self):
        # Exhaustively check all flag bitmasks 0..63 and sample edge values
        for seed in range(10):
            rnd = random.Random(seed)
            s = rnd.choice([8, 16, 32])
            for flags in range(64):
                for edge in [0, 1, 2, 5, 10, 50, 100, 255]:
                    res = R._kind_of(flags, edge, s)
                    if flags & R.FLAG_TEXTURE:
                        self.assertEqual(res, 1)
                    elif flags & R.FLAG_EDGE:
                        self.assertEqual(res, 0)
                    else:
                        expected = 2 if edge * s * 8 < 255 else 3
                        self.assertEqual(res, expected)

    def test_kind_of_negative_zero_edge(self):
        self.assertEqual(R._kind_of(0, 0, 16), 2)  # SMOOTH
        self.assertEqual(R._kind_of(0, 0, 8), 2)   # SMOOTH with edge=0


class TestRunsProps(unittest.TestCase):
    def test_runs_seeds_0_to_9(self):
        for seed in range(10):
            rnd = random.Random(seed)
            n = rnd.randint(1, 100)
            ms = sorted(set(rnd.randint(0, 1000) for _ in range(n)))
            runs = R._runs(ms)

            # Check concatenation recovers exact input list
            concat = [m for a, b in runs for m in range(a, b + 1)]
            self.assertEqual(concat, ms)

            # Check maximal consecutive runs invariant
            for i, (a, b) in enumerate(runs):
                self.assertLessEqual(a, b)
                if i > 0:
                    prev_b = runs[i - 1][1]
                    self.assertGreater(a, prev_b + 1)

    def test_runs_negative_empty(self):
        self.assertEqual(R._runs([]), [])


class TestIslandsProps(unittest.TestCase):
    def test_islands_seeds_0_to_9(self):
        for seed in range(10):
            rnd = random.Random(seed)
            gw, gh = 20, 20
            n_cells = rnd.randint(5, 40)
            cell_coords = set()
            while len(cell_coords) < n_cells:
                cell_coords.add((rnd.randrange(gw), rnd.randrange(gh)))

            cells = {c: layout.morton(c[0], c[1]) for c in cell_coords}

            res_islands = R._islands(cells)

            # Check exact partitioning of input cells
            all_res_cells = [c for isl in res_islands for c in isl]
            self.assertEqual(sorted(all_res_cells), sorted(cells.keys()))
            self.assertEqual(len(all_res_cells), len(set(all_res_cells)))

            # Compare against BFS reference implementation
            bfs_comps = _bfs_islands(cells)

            # Label each island by minimum Morton code
            res_norm = sorted([sorted(isl) for isl in res_islands], key=lambda isl: min(cells[c] for c in isl))
            bfs_norm = sorted([sorted(comp) for comp in bfs_comps], key=lambda comp: min(cells[c] for c in comp))

            self.assertEqual(res_norm, bfs_norm)

            for isl in res_islands:
                min_m = min(cells[c] for c in isl)
                self.assertIn(min_m, [cells[c] for c in isl])

    def test_islands_negative_empty(self):
        self.assertEqual(R._islands({}), [])


class TestAlignedProps(unittest.TestCase):
    def test_aligned_seeds_0_to_9(self):
        for seed in range(10):
            rnd = random.Random(seed)
            tx0 = rnd.randint(0, 255)
            ty0 = rnd.randint(0, 255)
            tx1 = rnd.randint(tx0, 255)
            ty1 = rnd.randint(ty0, 255)

            res = R._aligned(tx0, ty0, tx1, ty1)
            ref = grid.aligned_block((tx0, ty0, tx1, ty1))
            self.assertEqual(res, ref)

    def test_aligned_negative_out_of_range(self):
        with self.assertRaises(VqaError) as e:
            R._aligned(256, 256, 256, 256)
        self.assertEqual(e.exception.code, "E_VC_ID_RANGE")


if __name__ == "__main__":
    unittest.main()
