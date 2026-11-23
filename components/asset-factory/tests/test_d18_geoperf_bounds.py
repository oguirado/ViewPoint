"""Compare the compiled GEO filters and exact traversal on identical cells."""
from fractions import Fraction as F
from math import ceil, nextafter, sqrt
import os
from random import Random
from types import FunctionType
import unittest
from unittest.mock import patch

import numpy as np
from numba import get_num_threads, njit, objmode, prange, set_num_threads

from tools.mesh import certificate as geo
from tools.mesh import certificate_fast as fast


class BatchedBounds(unittest.TestCase):
    def assert_bounds(self, target, cells):
        tree = geo._TriangleBVH(target, geo.Work(10**8))
        batch = fast.Batch(tree.root).evaluate(cells)
        accelerated_count = sum(row is not None and row[1] > 0 for row in batch)
        self.assertGreaterEqual(accelerated_count, len(cells)//2)
        for ordinal, (cell, accelerated) in enumerate(zip(cells, batch, strict=True)):
            expected_work = geo.Work(10**8)
            expected = tree.cell_bounds(cell, expected_work)
            actual_work = geo.Work(10**8)
            actual = fast.resolve(tree, cell, actual_work, accelerated)
            self.assertEqual(actual, expected, ordinal)
            self.assertEqual(actual_work.used, expected_work.used, ordinal)
        return accelerated_count

    def test_random_rational_cells(self):
        random = Random(704113)
        total_compiled = 0
        for denominator in (1, 3, 128, 1024):
            def triangle(offset):
                return tuple(tuple(F(offset + random.randrange(-17, 18), denominator)
                                   for _ in range(3)) for _ in range(3))

            target = [triangle(i * 9) for i in range(32)]
            target += [target[0], (target[1][0],) * 3]
            cells = [triangle(random.randrange(280)) for _ in range(512)]
            total_compiled += self.assert_bounds(target, cells)
        self.assertGreaterEqual(total_compiled, 1500)

    def test_near_triangle_loss_is_observable(self):
        cell = ((0, 0, 1), (2, 0, 1), (0, 2, 1))
        near = ((0, 0, 0), (2, 0, 0), (0, 2, 0))
        far = tuple((x, y, z + 20) for x, y, z in near)
        self.assert_bounds([near, far], [cell])
        tree = geo._TriangleBVH([near, far], geo.Work(100))
        expected = tree.cell_bounds(cell, geo.Work(100))
        mutant = geo._TriangleBVH([far], geo.Work(100))
        changed = fast.resolve(mutant, cell, geo.Work(100), fast.Batch(mutant.root).evaluate([cell])[0])
        self.assertEqual(expected, (1, 1))
        self.assertNotEqual(changed, expected)
        result = fast.Batch(tree.root).evaluate([cell])[0]
        exact_cap, compiled_cap = geo.Work(5), geo.Work(5)
        with self.assertRaisesRegex(geo.InputIssue, "geometry_work_budget"):
            tree.cell_bounds(cell, exact_cap)
        with self.assertRaisesRegex(geo.InputIssue, "geometry_work_budget"):
            fast.resolve(tree, cell, compiled_cap, result)
        self.assertEqual(compiled_cap.used, exact_cap.used)
        with self.assertRaisesRegex(geo.InputIssue, "negative:witness"):
            geo.decide(*changed, 1, "negative")

    def assert_exact_cell(self, tree, cell):
        batch = fast.Batch(tree.root)
        self.assertTrue(batch._exact)
        result = batch.evaluate([cell])[0]
        exact_work, actual_work = geo.Work(100), geo.Work(100)
        expected = tree.cell_bounds(cell, exact_work)
        self.assertEqual(fast.resolve(tree, cell, actual_work, result), expected)
        self.assertEqual(actual_work.used, exact_work.used)
        return result[1]

    def test_exact_coordinate_near_tie_preserves_pruning(self):
        for scale in map(F, (1, 1000, 1000000)):
            with self.subTest(scale=scale):
                point = (F(0),) * 3
                loose = ((scale, 0, 0), (scale, scale, 0),
                         (scale / 2, 4 * scale, scale / 64))
                loose_distance = geo.triangle_d2(point, loose)
                closer = nextafter(sqrt(loose_distance), 0)
                self.assertLess(F(closer) ** 2, loose_distance)
                height = scale / 2
                closer = F(closer)
                plane = ((closer, -height, -height), (closer, height, -height),
                         (closer, 0, 2 * height))
                self.assertEqual(geo.triangle_d2(point, plane), closer ** 2)
                left_filler = ((-4 * scale, 4 * scale, 0),) * 3
                right_filler = ((4 * scale, 4 * scale, 0),) * 3
                target = [left_filler] * 3 + [loose, plane] + [right_filler] * 4
                tree = geo._TriangleBVH(target, geo.Work(100))
                self.assertEqual(self.assert_exact_cell(tree, (point,) * 3), -1)

    def test_exact_coordinate_sibling_near_tie_preserves_order(self):
        for scale in (1, 1000, 1000000):
            with self.subTest(scale=scale):
                point = (F(0),) * 3
                near = ((F(scale), 0, 0),) * 3
                far = ((F(nextafter(scale, float("inf"))), 0, 0),) * 3
                tree = geo._TriangleBVH([near] * 4 + [far] * 5, geo.Work(100))
                # The nearer child is left. Overlapping intervals cannot
                # prove that order, so the kernel must replay this comparison.
                self.assertEqual(self.assert_exact_cell(tree, (point,) * 3), -2)

    def test_outward_interval_contains_exact_distance(self):
        random = Random(63012)
        points = [(F(2, 3), F(5, 7), F(7, 11)),
                  (F(1, 3) + F(1, 2**54), F(1, 3), F(1, 3))]
        center = (F(1, 3),) * 3
        cases = [(point, (center,)*3) for point in points]
        for _ in range(512):
            vertices = [tuple(F(random.randint(-500, 500), 3) for _ in range(3))
                        for _ in range(4)]
            cases.append((vertices[0], tuple(vertices[1:])))
        for scale in (1, 1000, 1000000):
            point = (F(0),) * 3
            cases.append((point, ((F(nextafter(scale, 0)), F(3, 8), F(-1, 2)),) * 3))
        for point, tri in cases:
            with self.subTest(point=point, triangle=tri):
                self.assert_outward_interval(point, tri)

    def assert_outward_interval(self, point, tri):
        values = np.array((point, *tri), dtype=np.float64)
        scale = max(1., np.max(np.abs(values)))
        pad = np.nextafter(scale * fast._UNIT_PAD, np.inf)
        exact_coordinates = all(F(value) == coordinate
                                for row, original in zip(values, (point, *tri))
                                for value, coordinate in zip(row, original))
        lower, upper = fast._triangle_interval(values[0], values[1:], pad, exact_coordinates)
        exact = geo.triangle_d2(point, tri)
        self.assertLessEqual(F(lower), exact)
        self.assertGreaterEqual(F(upper), exact)
        box = geo._box(tri)
        low_box, high_box = fast._box_interval(values[[0, 0, 0]],
                                              np.array(box, dtype=float), pad, exact_coordinates)
        exact_box = sum(max(lo-x, x-hi, 0)**2 for x, (lo, hi) in zip(point, box))
        self.assertLessEqual(F(low_box[0]), exact_box)
        self.assertGreaterEqual(F(high_box[0]), exact_box)

    def test_original_geo_oracles_with_fast_path_forced(self):
        original = unittest.defaultTestLoader.loadTestsFromNames(
            ("test_d18_geo_certificate", "test_d18_geo_rp1"))
        result = unittest.TestResult()
        evaluate = fast.Batch.evaluate
        completed_cells = 0

        def observed(batch, cells):
            nonlocal completed_cells
            rows = evaluate(batch, cells)
            completed_cells += sum(row is not None and row[1] > 0 for row in rows)
            return rows

        with patch.object(geo, "_BATCH_MIN_CELLS", 1), patch.object(fast.Batch, "evaluate", observed):
            original.run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual(result.testsRun, 36)
        self.assertFalse(result.skipped)
        self.assertGreater(completed_cells, 0, "GEO controls did no compiled traversal")

    def test_warm_pool_uses_callers_narrowed_affinity(self):
        allowed = os.sched_getaffinity(0)
        cpus = sorted(allowed)
        self.assertGreaterEqual(len(cpus), 3)
        narrowed = set(cpus[:2])
        target = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        tree = geo._TriangleBVH([target], geo.Work(100))
        batch = fast.Batch(tree.root)
        cells = [target] * 8

        wide_cpus = tuple(cpus)
        narrow_cpus = tuple(cpus[:2])

        @njit
        def placement():
            with objmode(observed="int64"):
                actual = tuple(sorted(os.sched_getaffinity(0)))
                observed = 1 if actual == wide_cpus else 2 if actual == narrow_cpus else 0
            return observed

        @njit
        def observe_affinity(cell, boxes, branches, triangles, pad, exact, winners):
            return 0, placement()

        @njit(parallel=True)
        def observe_unpinned():
            observed = np.empty(2, dtype=np.int64)
            for i in prange(len(observed)):
                observed[i] = placement()
            return observed

        # Compile the real parallel loop with a witness in the place where
        # geometric work normally runs, including on already-warm workers.
        def substitute(dispatcher, **overrides):
            func = dispatcher.py_func
            return FunctionType(func.__code__, dict(func.__globals__, **overrides))

        chunk = njit(substitute(fast._pinned_chunk, _shortlist=observe_affinity))
        instrumented = njit(parallel=True)(substitute(fast._batch, _pinned_chunk=chunk))
        caller_threads = get_num_threads()
        self.assertGreaterEqual(caller_threads, 2)
        try:
            set_num_threads(2)
            with patch.object(fast, "_batch", instrumented):
                self.assertEqual({row[1] for row in batch.evaluate(cells)}, {1})
                os.sched_setaffinity(0, narrowed)
                self.assertEqual(set(observe_unpinned()), {1, 2})
                self.assertEqual({row[1] for row in batch.evaluate(cells)}, {2})
                self.assertEqual(set(observe_unpinned()), {1, 2})
                self.assertEqual(os.sched_getaffinity(0), narrowed)
        finally:
            os.sched_setaffinity(0, allowed)
            set_num_threads(caller_threads)

    def test_area_reduction_exact_at_rounding_boundary(self):
        reduction = geo._SquaredMeans()
        for sign in (-1, 0, 1):
            delta = F(sign, 1 << 100)
            complete = F(1, 10**6) + delta
            terms = [complete.as_integer_ratio()]
            reduction.directions.clear()
            reduction.record(1, 1, terms)
            reduction.record(1, 1, terms)
            self.assertEqual(reduction.ppm(1), ceil(complete*10**6))

    def test_directed_refinement_integral_and_budget(self):
        a = (0, 0, 0)
        b = (4, 0, 0)
        c = (0, 4, 0)
        d = (4, 4, 0)
        source = [(a, b, d)] * 80
        target = [(a, b, c), (b, c, d)]
        for budget in (300, 2000, 10**6):
            def result(work, integral):
                try:
                    return geo.directed_bounds(source, target, F(1, 2), 2, work, integral=integral)
                except geo.InputIssue as issue:
                    return issue.rc, issue.code
            reference = geo.Work(budget)
            observed = geo.Work(budget)
            reference_integral = []
            observed_integral = []
            with patch.object(geo, "_BATCH_MIN_CELLS", len(source) + 1):
                expected = result(reference, reference_integral)
            with patch.object(geo, "_BATCH_MIN_CELLS", 1):
                actual = result(observed, observed_integral)
            self.assertEqual((actual, observed.used, observed_integral),
                             (expected, reference.used, reference_integral))
            if observed_integral:
                mean = geo._SquaredMeans()
                for _ in range(2):
                    geo.directed_bounds(source, target, F(1, 2), 2, geo.Work(budget), integral=mean)
                self.assertEqual(mean.ppm(1), ceil(observed_integral[0]*10**6))


if __name__ == "__main__":
    unittest.main()
