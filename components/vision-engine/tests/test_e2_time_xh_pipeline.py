"""Lazy pipeline hashing and exact-period masking (SPEC_INT I2)."""
import unittest
from unittest import mock

import numpy as np

from tests.test_e1_change_pipeline import source
from vadapt.synth import synthetic as syn
from visioncore import desc, gop, parallel, pipeline
from visioncore.grid import Grid


class TestPeriodLabels(unittest.TestCase):
    def test_stale_label_cannot_start_period(self):
        grid = Grid(16, 16)
        for clean in (0, 1):
            for position in (0, 1, 2, 3):
                with self.subTest(clean=clean, position=position):
                    tiles = np.zeros((4, 1), dtype=desc.TILE_DTYPE)
                    tiles["xh64"][:, 0] = [10, 20, 10, 20]
                    tiles["cls"] = 2
                    tiles["cls"][position] = clean
                    self.assertEqual(gop.find_cycles(list(range(4)), tiles, grid), [])

    def test_first_frame_exact_hash_survives_clean_class(self):
        grid = Grid(16, 16)
        tiles = np.zeros((16, 1), dtype=desc.TILE_DTYPE)
        tiles["xh64"][:, 0] = np.arange(16) % 2
        tiles["cls"] = 4
        tiles["cls"][0] = 0
        cycles = gop.find_cycles(list(range(16)), tiles, grid, first_hash_exact=True)
        self.assertEqual([(c["t"], c["p"], c["n"]) for c in cycles],
                         [("f0:t0000", 2, 8)])

    def test_flicker_pipeline_keeps_first_cycle(self):
        run = pipeline.analyze(source("flicker"))
        cycles = run.gops_raw[0].cycles
        self.assertTrue(cycles)
        self.assertTrue(all(c["t"].startswith("f0:") and c["n"] == 8 for c in cycles))

    def test_later_gop_does_not_attest_copied_hashes(self):
        real = gop.find_cycles
        with mock.patch.object(gop, "find_cycles", wraps=real) as spy:
            pipeline.analyze(source("clean_static", n=48))
        self.assertGreater(len(spy.call_args_list), 1)
        self.assertEqual([c.kwargs["first_hash_exact"] for c in spy.call_args_list],
                         [True] + [False] * (len(spy.call_args_list) - 1))

    def test_exact_dirty_periods_survive(self):
        grid = Grid(16, 16)
        for period in range(2, 9):
            tiles = np.zeros((period * 3, 1), dtype=desc.TILE_DTYPE)
            tiles["xh64"][:, 0] = np.arange(period * 3) % period
            tiles["cls"] = 2
            cycles = gop.find_cycles(list(range(len(tiles))), tiles, grid)
            self.assertEqual([c["p"] for c in cycles], [period])


class TestLazyPipeline(unittest.TestCase):
    def test_pipeline_hashes_only_after_classification(self):
        real = desc._hash_tiles
        counts = []

        def spy(padded, grid, idx):
            counts.append(len(idx))
            return real(padded, grid, idx)

        with mock.patch.object(desc, "_hash_tiles", spy):
            run = pipeline.analyze(source("gpu_jitter"))
        self.assertEqual(counts, [run.grid.N])
        for tiles in run.tiles[1:]:
            self.assertEqual(tiles["xh64"].tobytes(), run.tiles[0]["xh64"].tobytes())

    def test_parallel_lazy_skips_hash_and_preserves_other_fields(self):
        src = source("gpu_jitter")
        frames = parallel.frames_array(src)
        grid = Grid(256, 160)
        eager = parallel.describe_many(frames, grid, workers=1)
        with mock.patch.object(desc, "_hash_tiles", side_effect=AssertionError("eager hash")):
            serial = parallel.describe_many(frames, grid, workers=1, lazy_xh=True)
            forked = parallel.describe_many(frames, grid, workers=2, cpus=[], lazy_xh=True)
        self.assertEqual(serial.tobytes(), forked.tobytes())
        self.assertFalse(serial["xh64"].any())
        eager["xh64"] = 0
        self.assertEqual(eager.tobytes(), serial.tobytes())

    def test_serial_parallel_blobs_equal_all_scenarios(self):
        for scenario in syn.SCENARIOS:
            with self.subTest(scenario=scenario):
                serial = pipeline.analyze(source(scenario))
                forked = pipeline.analyze(source(scenario), workers=2, cpus=[])
                self.assertEqual(serial._blobs(with_vision=False), forked._blobs(with_vision=False))

    def test_eager_equivalence_except_copied_hashes(self):
        describe = desc.describe_padded

        def eager(*args, **kwargs):
            kwargs["lazy_xh"] = False
            return describe(*args, **kwargs)

        for scenario in syn.SCENARIOS:
            with self.subTest(scenario=scenario):
                lazy = pipeline.analyze(source(scenario))
                with mock.patch.object(desc, "describe_padded", eager), \
                        mock.patch.object(desc, "fill_xh64", lambda *a, **k: None):
                    reference = pipeline.analyze(source(scenario))
                for a, b in zip(lazy.tiles, reference.tiles):
                    for field in a.dtype.names:
                        if field != "xh64":
                            self.assertEqual(a[field].tobytes(), b[field].tobytes(), field)
                    dirty = a["cls"] >= 2
                    self.assertEqual(a["xh64"][dirty].tobytes(), b["xh64"][dirty].tobytes())
                self.assertEqual(lazy.tiles[0].tobytes(), reference.tiles[0].tobytes())
                self.assertEqual(lazy.regions, reference.regions)
                self.assertEqual(lazy.links, reference.links)
                self.assertEqual(lazy.global_mv, reference.global_mv)
                for a, b in zip(lazy.frame_rows, reference.frame_rows):
                    self.assertEqual({k: v for k, v in a.items() if k != "tiles_sha256"},
                                     {k: v for k, v in b.items() if k != "tiles_sha256"})
                for a, b in zip(lazy.gops_raw, reference.gops_raw):
                    self.assertEqual(a.cycles, b.cycles)
                    self.assertEqual(a.tracks, b.tracks)
                    self.assertEqual(a.delta_bytes, b.delta_bytes)
                if scenario in ("clean_static", "flicker", "frozen_subregion", "small_overlap"):
                    self.assertEqual(lazy._blobs(False), reference._blobs(False))
