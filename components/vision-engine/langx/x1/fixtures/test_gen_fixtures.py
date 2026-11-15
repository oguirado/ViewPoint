#!/usr/bin/env python3
import json
import os
import shutil
import tempfile
import unittest

from check_fixtures import check_all, damage_k2_ref
from gen_fixtures import generate_all


class TestGenFixtures(unittest.TestCase):

    def test_generation_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
            # Change directory or pass base_dir
            import gen_fixtures
            orig_open = gen_fixtures.open

            # Helper to generate in directory
            def gen_in_dir(d):
                files = {}
                def custom_open(path, mode="r", *args, **kwargs):
                    fname = os.path.basename(path)
                    target = os.path.join(d, fname)
                    return orig_open(target, mode, *args, **kwargs)

                gen_fixtures.open = custom_open
                try:
                    gen_fixtures.generate_all()
                finally:
                    gen_fixtures.open = orig_open

            gen_in_dir(tmpdir1)
            gen_in_dir(tmpdir2)

            for fname in ["repack_cases.json", "repack_src.bin", "repack_dst.bin", "damage_cases.json"]:
                with open(os.path.join(tmpdir1, fname), "rb") as f1:
                    b1 = f1.read()
                with open(os.path.join(tmpdir2, fname), "rb") as f2:
                    b2 = f2.read()
                self.assertEqual(b1, b2, f"File {fname} generation is not deterministic")

    def test_worked_check_values(self):
        # Worked check: W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words;
        # a 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0.
        W, H, s = 1920, 1080, 32
        rects = [[96, 0, 192, 32]]

        gw = (W + s - 1) // s
        gh = (H + s - 1) // s
        total_tiles = gw * gh
        num_words = (total_tiles + 63) // 64

        self.assertEqual(gw, 60)
        self.assertEqual(gh, 34)
        self.assertEqual(total_tiles, 2040)
        self.assertEqual(num_words, 32)

        err, words_hex, popcount = damage_k2_ref(W, H, s, rects)
        self.assertIsNone(err)
        self.assertEqual(len(words_hex), 32)
        self.assertEqual(popcount, 6) # tiles 3, 4, 5, 6, 7, 8 -> 6 tiles

        word0 = int(words_hex[0], 16)
        # tiles 3, 4, 5, 6, 7, 8 set bit 3, 4, 5, 6, 7, 8
        expected_bits = sum(1 << b for b in range(3, 9))
        self.assertEqual(word0, expected_bits)

    def test_checker_rejects_flipped_byte(self):
        fixtures_dir = os.path.dirname(os.path.abspath(__file__))
        with tempfile.TemporaryDirectory() as tmpdir:
            # Copy all files from fixtures_dir to tmpdir
            for fname in ["gen_fixtures.py", "check_fixtures.py", "repack_cases.json", "repack_src.bin", "repack_dst.bin", "damage_cases.json"]:
                shutil.copy(os.path.join(fixtures_dir, fname), os.path.join(tmpdir, fname))

            # Confirm clean check passes
            ok, mismatches = check_all(tmpdir)
            self.assertTrue(ok, f"Clean check failed: {mismatches}")

            # Flip one byte in repack_src.bin
            src_bin_path = os.path.join(tmpdir, "repack_src.bin")
            with open(src_bin_path, "rb") as f:
                data = bytearray(f.read())
            data[0] ^= 0xFF
            with open(src_bin_path, "wb") as f:
                f.write(data)

            # Confirm checker rejects it
            ok, mismatches = check_all(tmpdir)
            self.assertFalse(ok)
            self.assertTrue(any("mismatch" in m or "identical" in m for m in mismatches), f"Mismatches: {mismatches}")

    def test_checker_rejects_wrong_popcount(self):
        fixtures_dir = os.path.dirname(os.path.abspath(__file__))
        with tempfile.TemporaryDirectory() as tmpdir:
            for fname in ["gen_fixtures.py", "check_fixtures.py", "repack_cases.json", "repack_src.bin", "repack_dst.bin", "damage_cases.json"]:
                shutil.copy(os.path.join(fixtures_dir, fname), os.path.join(tmpdir, fname))

            # Modify popcount in damage_cases.json
            damage_json_path = os.path.join(tmpdir, "damage_cases.json")
            with open(damage_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Find a non-error case and alter popcount
            for case in data["cases"]:
                if "expected_popcount" in case:
                    case["expected_popcount"] += 1
                    break

            with open(damage_json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")

            # Confirm checker rejects it
            ok, mismatches = check_all(tmpdir)
            self.assertFalse(ok)
            self.assertTrue(any("expected_popcount mismatch" in m or "identical" in m for m in mismatches), f"Mismatches: {mismatches}")


if __name__ == "__main__":
    unittest.main()
