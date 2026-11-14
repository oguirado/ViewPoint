"""Shared claim tests for screen fingerprint variants in eval/v2x (sf_*).

Discovers all screen-fingerprint variants dynamically using pkgutil and verifies
compliance with API signatures, mathematical properties, determinism, frame
immutability, and adversarial attacks built in RAM from fixed seeds.
"""

import importlib
import pkgutil
import sys
import unittest
import numpy as np

import eval.v2x


def discover_sf_modules():
    """Discover all screen fingerprint modules in eval.v2x matching prefix sf_."""
    sf_mods = []
    for _, name, _ in pkgutil.iter_modules(eval.v2x.__path__):
        if name.startswith("sf_"):
            mod = importlib.import_module(f"eval.v2x.{name}")
            sf_mods.append((name, mod))
    sf_mods.sort(key=lambda x: x[0])
    return sf_mods


# Variants with a known +1 global brightness fragility (B11): the claim is inverted for them until fixed.
KNOWN_BRIGHTNESS_DEFECTS = set()   # B11 fixed by #156 (2026-10-19)

class TestV2xSfClaims(unittest.TestCase):
    """Shared claim tests for all sf_* screen-fingerprint modules."""

    @classmethod
    def setUpClass(cls):
        cls.sf_mods = discover_sf_modules()
        print(
            f"\n[sf_claims] Discovered {len(cls.sf_mods)} screen fingerprint variant(s): "
            f"{[name for name, _ in cls.sf_mods]}",
            file=sys.stderr,
        )

    def test_discovered_variants_reported(self):
        """Verify sf_* variants are discovered and reported without failure on unknown modules."""
        self.assertGreater(
            len(self.sf_mods), 0, "Expected at least one sf_* module in eval/v2x"
        )
        for name, _ in self.sf_mods:
            print(f"[sf_claims] Reporting sf_ variant: {name}", file=sys.stderr)

    def test_sf_variants_claims(self):
        """Test API contract, distance properties, determinism, immutability, and attack cases."""
        for name, mod in self.sf_mods:
            with self.subTest(variant=name):
                # 1. API Existence & Return Types
                self.assertTrue(
                    hasattr(mod, "screen_fingerprint"),
                    f"Module {name} missing screen_fingerprint",
                )
                self.assertTrue(
                    hasattr(mod, "fingerprint_distance"),
                    f"Module {name} missing fingerprint_distance",
                )
                self.assertTrue(
                    hasattr(mod, "same_screen"),
                    f"Module {name} missing same_screen",
                )
                self.assertTrue(
                    hasattr(mod, "SF_THRESHOLD"),
                    f"Module {name} missing SF_THRESHOLD",
                )

                threshold = mod.SF_THRESHOLD
                self.assertIsInstance(
                    threshold, int, f"SF_THRESHOLD in {name} is not int"
                )
                self.assertNotIsInstance(
                    threshold, bool, f"SF_THRESHOLD in {name} is bool"
                )

                # Sample test frame
                rng = np.random.RandomState(42)
                f1 = rng.randint(0, 256, (600, 800, 3), dtype=np.uint8)
                f1.flags.writeable = False

                fp1 = mod.screen_fingerprint(f1)
                self.assertTrue(
                    isinstance(fp1, (tuple, str, int, bytes)),
                    f"Fingerprint {fp1} in {name} is not hashable/immutable type",
                )
                hash(fp1)  # must be hashable

                f2 = rng.randint(0, 256, (600, 800, 3), dtype=np.uint8)
                f2.flags.writeable = False
                fp2 = mod.screen_fingerprint(f2)

                dist = mod.fingerprint_distance(fp1, fp2)
                self.assertIsInstance(
                    dist, int, f"fingerprint_distance in {name} did not return int"
                )
                self.assertNotIsInstance(
                    dist, bool, f"fingerprint_distance in {name} returned bool"
                )
                self.assertGreaterEqual(
                    dist, 0, f"fingerprint_distance in {name} returned negative int"
                )

                same = mod.same_screen(fp1, fp2)
                self.assertIsInstance(
                    same, bool, f"same_screen in {name} did not return bool"
                )

                # 2. Distance properties: self-distance = 0, symmetry
                dist_self = mod.fingerprint_distance(fp1, fp1)
                self.assertEqual(
                    dist_self, 0, f"Distance to self in {name} is {dist_self} != 0"
                )

                dist_sym = mod.fingerprint_distance(fp2, fp1)
                self.assertEqual(
                    dist,
                    dist_sym,
                    f"Distance in {name} is asymmetric: d(fp1,fp2)={dist} != d(fp2,fp1)={dist_sym}",
                )

                # 3. same_screen agrees with distance <= SF_THRESHOLD
                expected_same = dist <= threshold
                self.assertEqual(
                    same,
                    expected_same,
                    f"same_screen ({same}) disagrees with (distance {dist} <= threshold {threshold}) in {name}",
                )

                # 4. Determinism across two calls
                fp1_again = mod.screen_fingerprint(f1)
                self.assertEqual(
                    fp1,
                    fp1_again,
                    f"screen_fingerprint in {name} is not deterministic across calls",
                )

                # 5. Input frame immutability
                f_mut = rng.randint(0, 256, (100, 100, 3), dtype=np.uint8)
                f_mut_orig = f_mut.copy()
                _ = mod.screen_fingerprint(f_mut)
                self.assertTrue(
                    np.array_equal(f_mut, f_mut_orig),
                    f"screen_fingerprint in {name} mutated the input frame",
                )

                # 6. Attack list (built in RAM from fixed seeds)

                # Attack A: 2 by 16 pixel caret toggled on and off is same_screen
                base_caret = rng.randint(50, 200, (600, 800, 3), dtype=np.uint8)
                caret_off = base_caret.copy()
                caret_on = base_caret.copy()
                caret_on[100:116, 200:202, :] = 255 - caret_on[100:116, 200:202, :]
                fp_coff = mod.screen_fingerprint(caret_off)
                fp_con = mod.screen_fingerprint(caret_on)
                self.assertTrue(
                    mod.same_screen(fp_coff, fp_con),
                    f"Attack Caret toggle failed in {name}: dist={mod.fingerprint_distance(fp_coff, fp_con)} > threshold {threshold}",
                )

                # Attack B: one tile of text changed is same_screen
                base_tile = rng.randint(50, 200, (600, 800, 3), dtype=np.uint8)
                tile_orig = base_tile.copy()
                tile_mod = base_tile.copy()
                tile_mod[100:116, 200:216, :] = rng.randint(0, 256, (16, 16, 3), dtype=np.uint8)
                fp_torig = mod.screen_fingerprint(tile_orig)
                fp_tmod = mod.screen_fingerprint(tile_mod)
                self.assertTrue(
                    mod.same_screen(fp_torig, fp_tmod),
                    f"Attack One tile changed failed in {name}: dist={mod.fingerprint_distance(fp_torig, fp_tmod)} > threshold {threshold}",
                )

                # Attack C: 4 pixel vertical scroll of a text page is same_screen
                text_page = np.full((1080, 1920, 3), 255, dtype=np.uint8)
                for y in range(50, 1000, 40):
                    text_page[y : y + 12, 100:1800, :] = 0
                text_scroll = text_page.copy()
                text_scroll[4:, :, :] = text_page[:-4, :, :]
                text_scroll[:4, :, :] = 255
                fp_txt = mod.screen_fingerprint(text_page)
                fp_scr = mod.screen_fingerprint(text_scroll)
                self.assertTrue(
                    mod.same_screen(fp_txt, fp_scr),
                    f"Attack 4px vertical scroll failed in {name}: dist={mod.fingerprint_distance(fp_txt, fp_scr)} > threshold {threshold}",
                )

                # Attack D: global brightness change of 1 code value is same_screen
                f_base_br = rng.randint(10, 245, (600, 800, 3), dtype=np.uint8)
                f_br1 = f_base_br.copy()
                f_br2 = (f_base_br.astype(np.int16) + 1).astype(np.uint8)
                fp_br1 = mod.screen_fingerprint(f_br1)
                fp_br2 = mod.screen_fingerprint(f_br2)
                if name in KNOWN_BRIGHTNESS_DEFECTS:   # real defect, tracked (v12p/BUGS_WEB_FOUND.md B11); must flip when fixed
                    self.assertFalse(mod.same_screen(fp_br1, fp_br2), f"{name} now survives a +1 brightness shift: remove it from KNOWN_BRIGHTNESS_DEFECTS")
                else:
                    self.assertTrue(
                        mod.same_screen(fp_br1, fp_br2),
                        f"Attack Global brightness shift failed in {name}: dist={mod.fingerprint_distance(fp_br1, fp_br2)} > threshold {threshold}",
                    )

                # Attack E: two different synthetic window layouts are different screens
                rng_w1 = np.random.RandomState(101)
                rng_w2 = np.random.RandomState(202)
                win1 = rng_w1.randint(0, 256, (600, 800, 3), dtype=np.uint8)
                win2 = rng_w2.randint(0, 256, (600, 800, 3), dtype=np.uint8)
                fp_w1 = mod.screen_fingerprint(win1)
                fp_w2 = mod.screen_fingerprint(win2)
                self.assertFalse(
                    mod.same_screen(fp_w1, fp_w2),
                    f"Attack Two window layouts failed in {name}: dist={mod.fingerprint_distance(fp_w1, fp_w2)} <= threshold {threshold}",
                )

                # Attack F: solid black and solid white frame are different screens
                f_black = np.zeros((600, 800, 3), dtype=np.uint8)
                f_white = np.full((600, 800, 3), 255, dtype=np.uint8)
                fp_blk = mod.screen_fingerprint(f_black)
                fp_wht = mod.screen_fingerprint(f_white)
                self.assertFalse(
                    mod.same_screen(fp_blk, fp_wht),
                    f"Attack Solid black vs solid white failed in {name}: dist={mod.fingerprint_distance(fp_blk, fp_wht)} <= threshold {threshold}",
                )

                # Attack G: 1x1 frame and 4K frame (3840x2160) do not crash
                f_1x1 = np.full((1, 1, 3), 128, dtype=np.uint8)
                f_4k = np.zeros((2160, 3840, 3), dtype=np.uint8)
                try:
                    fp_1x1 = mod.screen_fingerprint(f_1x1)
                    fp_4k = mod.screen_fingerprint(f_4k)
                except Exception as e:
                    self.fail(f"Attack 1x1 and 4K frame crashed in {name}: {e}")

                self.assertIsNotNone(fp_1x1)
                self.assertIsNotNone(fp_4k)


if __name__ == "__main__":
    unittest.main()
