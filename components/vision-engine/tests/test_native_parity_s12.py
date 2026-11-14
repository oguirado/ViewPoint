"""Tests for eval.native_parity_s12 host instrument."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import numpy as np

from eval import native_parity_s12
from visioncore import change, desc


class ReferenceShim:
    """Reference shim delegating directly to visioncore."""
    @staticmethod
    def _tile_sad_windows(y_cur, y_prev, c, s, tx, ty):
        return change._tile_sad_windows(y_cur, y_prev, c, s, tx, ty)

    @staticmethod
    def _tile_sad(y_cur, y_prev, c, s, gh, gw, tx, ty):
        return change._tile_sad(y_cur, y_prev, c, s, gh, gw, tx, ty)

    @staticmethod
    def _tile_sad_clip(y_cur, y_prev, c, s, gh, gw, tx, ty):
        return change._tile_sad_clip(y_cur, y_prev, c, s, gh, gw, tx, ty)

    @staticmethod
    def global_vector(y_cur, y_prev, s):
        return change.global_vector(y_cur, y_prev, s)

    @staticmethod
    def _tile_sums(absdiff, s, gh, gw):
        return change._tile_sums(absdiff, s, gh, gw)

    @staticmethod
    def _islands(mask):
        return change._islands(mask)

    @staticmethod
    def island_vector(lab_mask, y_cur, y_prev, s):
        return change.island_vector(lab_mask, y_cur, y_prev, s)

    @staticmethod
    def describe_padded_tiles(padded, grid):
        tiles, _ = desc.describe_padded(padded, grid)
        return tiles


class TestNativeParityS12(unittest.TestCase):
    def test_reference_shim_passes(self):
        rep = native_parity_s12.parity(ReferenceShim, seed=42)
        self.assertEqual(rep["status"], "OK")
        for name in native_parity_s12.NAMES:
            self.assertIn(name, rep["results"])
            res = rep["results"][name]
            self.assertEqual(res["status"], "OK")
            self.assertGreater(res["cases"], 0)
            self.assertEqual(res["cases"], res["equal"])
            self.assertIsNone(res["first_mismatch"])

    def test_missing_name_reports_absent(self):
        class MissingShim:
            _tile_sad_windows = ReferenceShim._tile_sad_windows
            # _tile_sad intentionally omitted
            _tile_sad_clip = ReferenceShim._tile_sad_clip
            global_vector = ReferenceShim.global_vector
            _tile_sums = ReferenceShim._tile_sums
            _islands = ReferenceShim._islands
            island_vector = ReferenceShim.island_vector
            describe_padded_tiles = ReferenceShim.describe_padded_tiles

        rep = native_parity_s12.parity(MissingShim, seed=42)
        self.assertEqual(rep["status"], "FAIL")
        self.assertEqual(rep["results"]["_tile_sad"]["status"], "ABSENT")
        self.assertEqual(rep["results"]["_tile_sad"]["first_mismatch"], "ABSENT")

    def test_mutant_off_by_one_sad(self):
        class MutantOffByOne(ReferenceShim):
            @staticmethod
            def _tile_sad_windows(y_cur, y_prev, c, s, tx, ty):
                sad, cnt = change._tile_sad_windows(y_cur, y_prev, c, s, tx, ty)
                return sad + 1, cnt

        rep = native_parity_s12.parity(MutantOffByOne, seed=42)
        self.assertEqual(rep["status"], "FAIL")
        self.assertEqual(rep["results"]["_tile_sad_windows"]["status"], "MISMATCH")
        self.assertIsNotNone(rep["results"]["_tile_sad_windows"]["first_mismatch"])

    def test_mutant_swapped_dx_dy(self):
        class MutantSwappedDxDy(ReferenceShim):
            @staticmethod
            def global_vector(y_cur, y_prev, s):
                dx, dy = change.global_vector(y_cur, y_prev, s)
                return (dy, dx)

        rep = native_parity_s12.parity(MutantSwappedDxDy, seed=42)
        self.assertEqual(rep["status"], "FAIL")
        self.assertEqual(rep["results"]["global_vector"]["status"], "MISMATCH")
        self.assertIsNotNone(rep["results"]["global_vector"]["first_mismatch"])

    def test_mutant_dropped_last_island(self):
        class MutantDroppedIsland(ReferenceShim):
            @staticmethod
            def _islands(mask):
                lab, n = change._islands(mask)
                if n > 0:
                    lab = lab.copy()
                    lab[lab == n] = 0
                    return lab, n - 1
                return lab, n

        rep = native_parity_s12.parity(MutantDroppedIsland, seed=42)
        self.assertEqual(rep["status"], "FAIL")
        self.assertEqual(rep["results"]["_islands"]["status"], "MISMATCH")
        self.assertIsNotNone(rep["results"]["_islands"]["first_mismatch"])

    def test_mutant_flipped_descriptor_byte(self):
        class MutantFlippedDescriptorByte(ReferenceShim):
            @staticmethod
            def describe_padded_tiles(padded, grid):
                tiles, _ = desc.describe_padded(padded, grid)
                tiles = tiles.copy()
                if len(tiles) > 0:
                    raw = tiles.view(np.uint8)
                    raw[0] ^= 1
                return tiles

        rep = native_parity_s12.parity(MutantFlippedDescriptorByte, seed=42)
        self.assertEqual(rep["status"], "FAIL")
        self.assertEqual(rep["results"]["describe_padded_tiles"]["status"], "MISMATCH")
        self.assertIsNotNone(rep["results"]["describe_padded_tiles"]["first_mismatch"])

    def test_cli_execution_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_file = os.path.join(tmpdir, "test_shim.py")
            code = """
import numpy as np
from visioncore import change, desc

def _tile_sad_windows(y_cur, y_prev, c, s, tx, ty):
    return change._tile_sad_windows(y_cur, y_prev, c, s, tx, ty)

def _tile_sad(y_cur, y_prev, c, s, gh, gw, tx, ty):
    return change._tile_sad(y_cur, y_prev, c, s, gh, gw, tx, ty)

def _tile_sad_clip(y_cur, y_prev, c, s, gh, gw, tx, ty):
    return change._tile_sad_clip(y_cur, y_prev, c, s, gh, gw, tx, ty)

def global_vector(y_cur, y_prev, s):
    return change.global_vector(y_cur, y_prev, s)

def _tile_sums(absdiff, s, gh, gw):
    return change._tile_sums(absdiff, s, gh, gw)

def _islands(mask):
    return change._islands(mask)

def island_vector(lab_mask, y_cur, y_prev, s):
    return change.island_vector(lab_mask, y_cur, y_prev, s)

def describe_padded_tiles(padded, grid):
    tiles, _ = desc.describe_padded(padded, grid)
    return tiles
"""
            with open(mod_file, "w") as f:
                f.write(code)

            # Test passing CLI
            env = os.environ.copy()
            env["PYTHONPATH"] = f"{tmpdir}:{env.get('PYTHONPATH', '')}"
            proc = subprocess.run(
                [sys.executable, "-m", "eval.native_parity_s12", "--module", "test_shim"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, f"CLI stderr: {proc.stderr}")
            rep = json.loads(proc.stdout)
            self.assertEqual(rep["status"], "OK")

            # Test mutant CLI failure
            mutant_code = code + "\n" + """
def global_vector(y_cur, y_prev, s):
    dx, dy = change.global_vector(y_cur, y_prev, s)
    return (dy, dx)
"""
            with open(mod_file, "w") as f:
                f.write(mutant_code)

            proc_mut = subprocess.run(
                [sys.executable, "-m", "eval.native_parity_s12", "--module", "test_shim"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc_mut.returncode, 1)
            rep_mut = json.loads(proc_mut.stdout)
            self.assertEqual(rep_mut["status"], "FAIL")
            self.assertEqual(rep_mut["results"]["global_vector"]["status"], "MISMATCH")


if __name__ == "__main__":
    unittest.main()
