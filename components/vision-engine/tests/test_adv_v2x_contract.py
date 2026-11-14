"""Adversarial contract tests for eval/v2x variants (gm_*, fl_*, st_*).

Tests every postpass variant discovered dynamically at test time in eval/v2x.
Checks compliance with the return contract specified in eval/v2x/README.md and
verifies input immutability across a comprehensive attack suite.
"""

import importlib
import math
import pkgutil
import unittest
import numpy as np

import eval.v2x

# Explicit bug list table of (variant, attack, error_description)
# Every entry listed in KNOWN_VIOLATIONS must fail/violate, and every unlisted entry must pass.
KNOWN_VIOLATIONS = [
]


def discover_v2x_modules():
    """Discover all postpass variant modules in eval.v2x matching gm_, fl_, or st_."""
    gm_mods = []
    fl_mods = []
    st_mods = []
    for _, name, _ in pkgutil.iter_modules(eval.v2x.__path__):
        # gm_v2_oracle consumes the variant API as a conformance harness.
        if name.startswith("gm_") and name != "gm_v2_oracle":
            gm_mods.append((name, importlib.import_module(f"eval.v2x.{name}")))
        elif name.startswith("fl_"):
            fl_mods.append((name, importlib.import_module(f"eval.v2x.{name}")))
        elif name.startswith("st_"):
            st_mods.append((name, importlib.import_module(f"eval.v2x.{name}")))
    gm_mods.sort(key=lambda x: x[0])
    fl_mods.sort(key=lambda x: x[0])
    st_mods.sort(key=lambda x: x[0])
    return gm_mods, fl_mods, st_mods


def make_frame(H, W, val_type="random", non_contig=False, read_only=False, seed=42):
    """Generate in-RAM frame fixtures."""
    rng = np.random.RandomState(seed)
    if val_type == "zero":
        arr = np.zeros((H, W), dtype=np.uint8)
    elif val_type == "255":
        arr = np.full((H, W), 255, dtype=np.uint8)
    elif val_type == "random":
        arr = rng.randint(0, 256, size=(H, W), dtype=np.uint8)
    else:
        raise ValueError(f"Unknown val_type: {val_type}")

    if non_contig:
        big = np.zeros((H * 2, W * 2), dtype=np.uint8)
        big[::2, ::2] = arr
        arr = big[::2, ::2]

    if read_only:
        arr.flags.writeable = False

    return arr


class TestAdvV2xContract(unittest.TestCase):
    """Adversarial contract tests for postpass variants in eval/v2x."""

    @classmethod
    def setUpClass(cls):
        cls.gm_mods, cls.fl_mods, cls.st_mods = discover_v2x_modules()
        cls.known_map = {
            (var, att): err for var, att, err in KNOWN_VIOLATIONS
        }

    def _assert_or_violate(self, variant, attack, test_fn):
        """Execute test_fn; check pass/violation against KNOWN_VIOLATIONS."""
        err = None
        try:
            test_fn()
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        except AssertionError as e:
            err = f"AssertionError: {e}"

        known_err = self.known_map.get((variant, attack))
        if known_err is not None:
            self.assertIsNotNone(
                err,
                f"Variant {variant} under attack {attack} was expected to fail with "
                f"known violation '{known_err}', but it passed."
            )
        else:
            self.assertIsNone(
                err,
                f"Variant {variant} under attack {attack} failed unexpectedly: {err}"
            )

    def test_gm_variants_contract(self):
        """Test global motion (gm_*) variants against all relevant attacks."""
        for name, mod in self.gm_mods:
            attacks = [
                ("shape_1x1", (1, 1), "random", 16, 5),
                ("shape_1xW", (1, 33), "random", 16, 5),
                ("shape_Hx1", (17, 1), "random", 16, 5),
                ("shape_17x33", (17, 33), "random", 16, 5),
                ("tile_larger_than_frame", (10, 10), "random", 32, 5),
                ("max_d_zero", (17, 33), "random", 16, 0),
                ("identical_frames", (17, 33), "identical", 16, 5),
                ("all_0_luma", (17, 33), "zero", 16, 5),
                ("all_255_luma", (17, 33), "255", 16, 5),
                ("non_contig_readonly", (17, 33), "random_nc_ro", 16, 5),
            ]
            for attack, shape, vtype, tile_s, max_d in attacks:
                H, W = shape
                if vtype == "identical":
                    p = make_frame(H, W, "random", seed=1)
                    c = p.copy()
                elif vtype == "random_nc_ro":
                    p = make_frame(H, W, "random", non_contig=True, read_only=True, seed=1)
                    c = make_frame(H, W, "random", non_contig=True, read_only=True, seed=2)
                else:
                    p = make_frame(H, W, vtype, seed=1)
                    c = make_frame(H, W, vtype, seed=2)

                def run_gm_attack(mod=mod, p=p, c=c, max_d=max_d, tile_s=tile_s, H=H, W=W):
                    p_orig = p.copy()
                    c_orig = c.copy()

                    # 1. estimate_global_motion
                    res = mod.estimate_global_motion(p, c, max_d)
                    self.assertTrue(
                        np.array_equal(p, p_orig),
                        "prev_luma was mutated in estimate_global_motion"
                    )
                    self.assertTrue(
                        np.array_equal(c, c_orig),
                        "cur_luma was mutated in estimate_global_motion"
                    )

                    if res is not None:
                        self.assertIsInstance(
                            res, tuple,
                            f"estimate_global_motion return is not None or tuple: {type(res)}"
                        )
                        self.assertEqual(
                            len(res), 3,
                            f"estimate_global_motion return tuple length != 3: {len(res)}"
                        )
                        dx, dy, score = res
                        self.assertIsInstance(
                            dx, (int, np.integer),
                            f"dx is not integer type: {type(dx)}"
                        )
                        self.assertNotIsInstance(dx, bool, "dx is bool")
                        self.assertIsInstance(
                            dy, (int, np.integer),
                            f"dy is not integer type: {type(dy)}"
                        )
                        self.assertNotIsInstance(dy, bool, "dy is bool")
                        self.assertIsInstance(
                            score, (int, float, np.number),
                            f"score is not numeric type: {type(score)}"
                        )
                        self.assertNotIsInstance(score, bool, "score is bool")
                        self.assertTrue(
                            math.isfinite(score) and score >= 0,
                            f"score must be finite and nonnegative: {score}"
                        )

                    # 2. consensus_tiles
                    dx_eval = res[0] if res is not None else 0
                    dy_eval = res[1] if res is not None else 0
                    mask = mod.consensus_tiles(p, c, tile_s, dx_eval, dy_eval)
                    self.assertTrue(
                        np.array_equal(p, p_orig),
                        "prev_luma was mutated in consensus_tiles"
                    )
                    self.assertTrue(
                        np.array_equal(c, c_orig),
                        "cur_luma was mutated in consensus_tiles"
                    )

                    self.assertIsInstance(
                        mask, np.ndarray,
                        f"consensus_tiles return is not np.ndarray: {type(mask)}"
                    )
                    self.assertEqual(
                        mask.dtype, np.bool_,
                        f"consensus_tiles mask dtype is not bool: {mask.dtype}"
                    )
                    gh, gw = math.ceil(H / tile_s), math.ceil(W / tile_s)
                    self.assertEqual(
                        mask.shape, (gh, gw),
                        f"consensus_tiles mask shape {mask.shape} != expected {(gh, gw)}"
                    )

                self._assert_or_violate(name, attack, run_gm_attack)

    def test_st_variants_contract(self):
        """Test sub-tile motion (st_*) variants against all relevant attacks."""
        for name, mod in self.st_mods:
            attacks = [
                ("shape_1x1", (1, 1), "random", 16, (0, 0), 5),
                ("shape_1xW", (1, 33), "random", 16, (0, 0), 5),
                ("shape_Hx1", (17, 1), "random", 16, (0, 0), 5),
                ("shape_17x33", (17, 33), "random", 16, (0, 0), 5),
                ("tile_larger_than_frame", (10, 10), "random", 32, (0, 0), 5),
                ("max_d_zero", (17, 33), "random", 16, (0, 0), 0),
                ("identical_frames", (17, 33), "identical", 16, (0, 0), 5),
                ("all_0_luma", (17, 33), "zero", 16, (0, 0), 5),
                ("all_255_luma", (17, 33), "255", 16, (0, 0), 5),
                ("non_contig_readonly", (17, 33), "random_nc_ro", 16, (0, 0), 5),
                ("tile_xy_last_partial", (17, 33), "random", 16, "last_partial", 5),
            ]
            for attack, shape, vtype, tile_s, tile_xy, max_d in attacks:
                H, W = shape
                gh, gw = math.ceil(H / tile_s), math.ceil(W / tile_s)
                if tile_xy == "last_partial":
                    tile_xy = (gw - 1, gh - 1)

                if vtype == "identical":
                    p = make_frame(H, W, "random", seed=1)
                    c = p.copy()
                elif vtype == "random_nc_ro":
                    p = make_frame(H, W, "random", non_contig=True, read_only=True, seed=1)
                    c = make_frame(H, W, "random", non_contig=True, read_only=True, seed=2)
                else:
                    p = make_frame(H, W, vtype, seed=1)
                    c = make_frame(H, W, vtype, seed=2)

                def run_st_attack(mod=mod, p=p, c=c, tile_s=tile_s, tile_xy=tile_xy, max_d=max_d):
                    p_orig = p.copy()
                    c_orig = c.copy()

                    res = mod.subtile_motion(p, c, tile_s, tile_xy, max_d)
                    self.assertTrue(
                        np.array_equal(p, p_orig),
                        "prev_luma was mutated in subtile_motion"
                    )
                    self.assertTrue(
                        np.array_equal(c, c_orig),
                        "cur_luma was mutated in subtile_motion"
                    )

                    if res is not None:
                        self.assertIsInstance(
                            res, tuple,
                            f"subtile_motion return is not None or tuple: {type(res)}"
                        )
                        self.assertEqual(
                            len(res), 3,
                            f"subtile_motion return tuple length != 3: {len(res)}"
                        )
                        dx, dy, n_blocks = res
                        self.assertIsInstance(
                            dx, (int, np.integer),
                            f"dx is not integer type: {type(dx)}"
                        )
                        self.assertNotIsInstance(dx, bool, "dx is bool")
                        self.assertIsInstance(
                            dy, (int, np.integer),
                            f"dy is not integer type: {type(dy)}"
                        )
                        self.assertNotIsInstance(dy, bool, "dy is bool")
                        self.assertIsInstance(
                            n_blocks, (int, np.integer),
                            f"n_blocks is not integer type: {type(n_blocks)}"
                        )
                        self.assertNotIsInstance(n_blocks, bool, "n_blocks is bool")

                        self.assertGreater(
                            n_blocks, 0,
                            f"subtile claims need positive block count: {n_blocks}"
                        )
                        self.assertFalse(
                            dx == 0 and dy == 0,
                            f"subtile claims need a nonzero vector: dx={dx}, dy={dy}"
                        )

                self._assert_or_violate(name, attack, run_st_attack)

    def test_fl_variants_contract(self):
        """Test flicker tracker (fl_*) variants against all relevant attacks."""
        for name, mod in self.fl_mods:
            attacks = [
                ("n_tiles_1", 1, 64, "sequence_toggle"),
                ("max_half_1", 4, 1, "sequence_toggle"),
                ("hashes_all_equal", 4, 64, "all_equal"),
                ("hashes_all_distinct", 4, 64, "all_distinct"),
                ("hashes_max_u64", 4, 64, "max_u64"),
                ("non_contig_readonly", 4, 64, "non_contig_ro"),
            ]
            for attack, n_tiles, max_half, hash_mode in attacks:
                def run_fl_attack(mod=mod, n_tiles=n_tiles, max_half=max_half, hash_mode=hash_mode):
                    cls = getattr(mod, "FlickerTracker", None)
                    self.assertIsNotNone(cls, f"Module {mod.__name__} missing FlickerTracker class")

                    tracker = cls(n_tiles=n_tiles, max_half=max_half)

                    for frame in range(10):
                        if hash_mode == "all_equal":
                            h = np.full(n_tiles, 100 + (frame % 2) * 50, dtype=np.uint64)
                        elif hash_mode == "all_distinct":
                            h = np.arange(n_tiles, dtype=np.uint64) * 1000 + frame
                        elif hash_mode == "max_u64":
                            val = 0xFFFFFFFFFFFFFFFF if (frame % 2) == 0 else 0x7FFFFFFFFFFFFFFF
                            h = np.full(n_tiles, val, dtype=np.uint64)
                        elif hash_mode == "non_contig_ro":
                            big = np.arange(n_tiles * 2, dtype=np.uint64) * 100 + frame
                            h = big[::2]
                            h.flags.writeable = False
                        else:  # sequence_toggle
                            h = np.full(n_tiles, 100 + (frame % 2) * 50, dtype=np.uint64)

                        h_orig = h.copy()
                        res = tracker.update(h)

                        self.assertTrue(
                            np.array_equal(h, h_orig),
                            "hashes input was mutated in update()"
                        )
                        self.assertIsInstance(
                            res, tuple,
                            f"update() return is not tuple: {type(res)}"
                        )
                        self.assertEqual(
                            len(res), 2,
                            f"update() return tuple length != 2: {len(res)}"
                        )
                        states, half_periods = res
                        self.assertIsInstance(
                            states, np.ndarray,
                            f"states is not np.ndarray: {type(states)}"
                        )
                        self.assertIsInstance(
                            half_periods, np.ndarray,
                            f"half_periods is not np.ndarray: {type(half_periods)}"
                        )
                        self.assertEqual(
                            states.shape, (n_tiles,),
                            f"states shape {states.shape} != expected ({n_tiles},)"
                        )
                        self.assertEqual(
                            half_periods.shape, (n_tiles,),
                            f"half_periods shape {half_periods.shape} != expected ({n_tiles},)"
                        )
                        self.assertTrue(
                            states.dtype.kind in ("i", "u"),
                            f"states dtype not integer: {states.dtype}"
                        )
                        self.assertTrue(
                            half_periods.dtype.kind in ("i", "u"),
                            f"half_periods dtype not integer: {half_periods.dtype}"
                        )

                        # Tracker states are class IDs in 0..6
                        self.assertTrue(
                            np.all((states >= 0) & (states <= 6)),
                            f"states out of class range 0..6: {states}"
                        )
                        # Half periods are integers in 0..max_half
                        self.assertTrue(
                            np.all((half_periods >= 0) & (half_periods <= max_half)),
                            f"half_periods out of range 0..{max_half}: {half_periods}"
                        )
                        # Positive for FLICKER claims (state == 4)
                        flicker_mask = (states == 4)
                        if np.any(flicker_mask):
                            self.assertTrue(
                                np.all(half_periods[flicker_mask] > 0),
                                f"half_periods must be positive for FLICKER claims: {half_periods[flicker_mask]}"
                            )

                self._assert_or_violate(name, attack, run_fl_attack)


if __name__ == "__main__":
    unittest.main()
