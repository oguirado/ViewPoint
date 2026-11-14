"""RAM-only screen identity and DEV evaluation contract regressions.

Synthetic pixel fixtures are generated here from fixed seeds. Temporary files
contain JSON metrics only; no frames, luma planes or binary fixtures are written.
"""

import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest import mock

import numpy as np

from eval import screen_id_eval
from eval.v2x import sf_tiledesc as sf
from visioncore.desc import describe
from visioncore.grid import Grid


VARIANT = "eval.v2x.sf_tiledesc"


def synthetic_layout():
    frame = np.full((128, 192, 3), 232, dtype=np.uint8)
    frame[:, :64] = (35, 43, 55)
    frame[:16] = (58, 75, 92)
    for top in (32, 64, 96):
        frame[top:top + 8, 80:176] = (110, 118, 126)
    return frame


def constant_variant(name="eval.v2x.sf_test_constant"):
    """An independent oracle for pair counting, deliberately merging all screens."""
    variant = ModuleType(name)
    variant.SF_THRESHOLD = 0
    variant.screen_fingerprint = lambda frame: b"constant"
    variant.fingerprint_distance = lambda left, right: 0
    variant.same_screen = lambda left, right: True
    return variant


class TestTileDescriptorFingerprint(unittest.TestCase):
    def test_identical_frame_zero_distance_and_python_types(self):
        frame = synthetic_layout()
        frame.flags.writeable = False
        left = sf.screen_fingerprint(frame)
        right = sf.screen_fingerprint(frame.copy())
        self.assertEqual(left, right)
        self.assertEqual(sf.fingerprint_distance(left, right), 0)
        self.assertIs(type(sf.fingerprint_distance(left, right)), int)
        self.assertIs(type(sf.same_screen(left, right)), bool)
        self.assertIs(type(sf.SF_THRESHOLD), int)
        self.assertTrue(sf.same_screen(left, right))
        self.assertEqual({left, right}, {left})

    def test_deterministic_across_interleaved_calls(self):
        frame = synthetic_layout()
        first = sf.screen_fingerprint(frame)
        sf.screen_fingerprint(np.full_like(frame, 17))
        self.assertEqual(first, sf.screen_fingerprint(frame))
        # The immutable fingerprint must not retain a view of the caller's RAM.
        frame[:] = 0
        self.assertEqual(first, sf.screen_fingerprint(synthetic_layout()))

    def test_input_pixels_shape_and_writeability_unchanged(self):
        rng = np.random.RandomState(71)
        backing = rng.randint(0, 256, (66, 98, 3)).astype(np.uint8)
        views = (backing, backing[::2, ::2], backing[::-1, ::-1])
        for view in views:
            for readonly in (False, True):
                with self.subTest(shape=view.shape, strides=view.strides, readonly=readonly):
                    candidate = view.view()
                    candidate.flags.writeable = not readonly
                    before = backing.copy()
                    shape, strides = candidate.shape, candidate.strides
                    fp = sf.screen_fingerprint(candidate)
                    self.assertEqual(sf.fingerprint_distance(fp, fp), 0)
                    np.testing.assert_array_equal(backing, before)
                    self.assertEqual(candidate.shape, shape)
                    self.assertEqual(candidate.strides, strides)
                    self.assertEqual(candidate.flags.writeable, not readonly)

    def test_one_tile_caret_change_is_same_screen(self):
        before = synthetic_layout()
        after = before.copy()
        after[50:61, 100:102] = 0
        grid = Grid(before.shape[1], before.shape[0])
        old_tiles, _ = describe(before, grid)
        new_tiles, _ = describe(after, grid)
        self.assertEqual(int(np.count_nonzero(old_tiles["xh64"] != new_tiles["xh64"])), 1)
        left, right = sf.screen_fingerprint(before), sf.screen_fingerprint(after)
        self.assertTrue(sf.same_screen(left, right))
        self.assertLessEqual(sf.fingerprint_distance(left, right), sf.SF_THRESHOLD)

    def test_different_layouts_with_identical_tile_bags_separate(self):
        vertical = np.full((128, 128, 3), 235, dtype=np.uint8)
        horizontal = vertical.copy()
        vertical[:, :64] = 20
        horizontal[:64] = 20
        grid = Grid(128, 128)
        a, _ = describe(vertical, grid)
        b, _ = describe(horizontal, grid)
        self.assertEqual(sorted(map(int, a["xh64"])), sorted(map(int, b["xh64"])))
        left, right = sf.screen_fingerprint(vertical), sf.screen_fingerprint(horizontal)
        distance = sf.fingerprint_distance(left, right)
        self.assertGreater(distance, sf.SF_THRESHOLD)
        self.assertEqual(distance, sf.fingerprint_distance(right, left))
        self.assertFalse(sf.same_screen(left, right))

    def test_partial_tiles_and_tiny_readonly_frames(self):
        for width, height in ((1, 1), (33, 1), (1, 17), (33, 17)):
            with self.subTest(width=width, height=height):
                frame = np.full((height, width, 3), (61, 93, 127), dtype=np.uint8)
                frame.flags.writeable = False
                fp = sf.screen_fingerprint(frame)
                self.assertIs(type(sf.fingerprint_distance(fp, fp)), int)
                self.assertEqual(sf.fingerprint_distance(fp, fp), 0)
                self.assertTrue(sf.same_screen(fp, fp))
                hash(fp)

    def test_subtile_scroll_keeps_screen_identity(self):
        rng = np.random.RandomState(91)
        world = rng.randint(30, 220, (112, 176, 3)).astype(np.uint8)
        first = sf.screen_fingerprint(world[:96, :160])
        for view in (world[:96, 5:165], world[7:103, :160]):
            with self.subTest(strides=view.strides, start=view[0, 0].tolist()):
                moved = sf.screen_fingerprint(view)
                self.assertTrue(sf.same_screen(first, moved))

    def test_alternating_one_level_aa_change_keeps_identity(self):
        rng = np.random.RandomState(92)
        base = rng.randint(30, 220, (96, 160, 3)).astype(np.int16)
        yy, xx = np.indices(base.shape[:2])
        sign = (((xx + yy) & 1) * 2 - 1)[..., None]
        left = sf.screen_fingerprint((base + sign).astype(np.uint8))
        right = sf.screen_fingerprint((base - sign).astype(np.uint8))
        self.assertTrue(sf.same_screen(left, right))


class TestScreenIdentityEvaluation(unittest.TestCase):
    def test_tiny_real_harness_two_kinds_three_frames(self):
        result = screen_id_eval.evaluate(
            [VARIANT], per_kind=2, frames=3, size=(96, 64),
            kinds=("static_clean", "caret_blink"),
        )
        self.assertTrue({"schema", "split", "config", "ground_truth", "variants", "timing"} <= result.keys())
        self.assertEqual(result["split"], "DEV")
        row = result["variants"][VARIANT]
        self.assertEqual(row["status"], "OK")
        self.assertTrue({"threshold", "same_screen_recall", "different_screen_separation",
                         "transitions", "per_kind", "kind_pairs", "fingerprint_size_bytes"} <= row.keys())
        self.assertEqual(row["same_screen_recall"]["pairs"], 12)
        self.assertEqual(row["different_screen_separation"]["pairs"], 6)
        self.assertEqual(set(row["per_kind"]), {"static_clean", "caret_blink"})

    def test_all_pairs_and_transition_exclusion_independent_oracle(self):
        variant = constant_variant()
        result = screen_id_eval.evaluate(
            [variant], per_kind=2, frames=3, size=(32, 24),
            kinds=("static_clean", "caret_blink", "scroll_page"),
        )
        row = result["variants"][variant.__name__]
        self.assertEqual(row["status"], "OK")
        # Four eligible scenes, three unordered pairs each; two transition
        # scenes must not enter this denominator. All six first frames pair up.
        self.assertEqual(row["same_screen_recall"]["pairs"], 12)
        self.assertEqual(row["same_screen_recall"]["correct"], 12)
        self.assertEqual(row["same_screen_recall"]["value_ppm"], 1_000_000)
        self.assertEqual(row["different_screen_separation"]["pairs"], 15)
        self.assertEqual(row["different_screen_separation"]["correct"], 0)
        self.assertEqual(row["different_screen_separation"]["value_ppm"], 0)
        self.assertIn("scroll_page", row["transitions"])
        self.assertEqual(row["transitions"]["scroll_page"]["pairs"], 6)
        self.assertTrue(row["transitions"]["excluded_from_recall"])
        self.assertIsNone(row["transitions"]["scroll_page"]["correct"])
        self.assertEqual(row["fingerprint_size_bytes"]["min"], len(b"constant"))
        self.assertEqual(row["fingerprint_size_bytes"]["max"], len(b"constant"))
        self.assertEqual(sum(pair["different_screen_separation"]["pairs"]
                             for pair in row["kind_pairs"]), 15)

    def test_undefined_metrics_stay_null(self):
        variant = constant_variant()
        result = screen_id_eval.evaluate(
            [variant], per_kind=1, frames=1, size=(16, 16), kinds=("scroll_large",),
        )
        row = result["variants"][variant.__name__]
        for key in ("same_screen_recall", "different_screen_separation"):
            self.assertEqual(row[key]["pairs"], 0)
            self.assertIsNone(row[key]["value_ppm"])

    def test_invalid_dev_budget_rejected_before_scene_build(self):
        for count in (0, -1, 65, True):
            with self.subTest(per_kind=count):
                with mock.patch("eval.gui_synth.scenes.build", side_effect=AssertionError("must validate before generation")):
                    with self.assertRaises(ValueError):
                        screen_id_eval.evaluate(
                            [constant_variant()], per_kind=count, frames=1,
                            size=(16, 16), kinds=("static_clean",),
                        )

    def test_failed_variant_keeps_good_variant_and_suppresses_partial_metrics(self):
        good = constant_variant()
        bad = constant_variant("eval.v2x.sf_test_bad_distance")
        bad.fingerprint_distance = lambda left, right: np.int64(0)
        result = screen_id_eval.evaluate(
            [bad, good], per_kind=1, frames=2, size=(16, 16), kinds=("static_clean",),
        )
        self.assertEqual(result["variants"][good.__name__]["status"], "OK")
        bad_row = result["variants"][bad.__name__]
        self.assertEqual(bad_row["status"], "FAILED")
        self.assertIn("error", bad_row)
        self.assertNotIn("same_screen_recall", bad_row)

    def test_mutable_fingerprint_is_rejected(self):
        bad = constant_variant("eval.v2x.sf_test_mutable")
        bad.screen_fingerprint = lambda frame: [1, 2, 3]
        result = screen_id_eval.evaluate(
            [bad], per_kind=1, frames=1, size=(16, 16), kinds=("static_clean",),
        )
        self.assertEqual(result["variants"][bad.__name__]["status"], "FAILED")

    def test_input_mutation_failure_does_not_contaminate_another_variant(self):
        bad = constant_variant("eval.v2x.sf_a_mutating")
        good = constant_variant("eval.v2x.sf_z_observing")

        def mutate_shape(frame):
            frame.shape = (frame.size,)
            return b"changed metadata"

        def observe(frame):
            self.assertEqual(frame.shape, (16, 16, 3))
            self.assertFalse(frame.flags.writeable)
            with self.assertRaises(ValueError):
                frame.flags.writeable = True
            return b"constant"

        bad.screen_fingerprint, good.screen_fingerprint = mutate_shape, observe
        result = screen_id_eval.evaluate(
            [bad, good], per_kind=1, frames=2, size=(16, 16), kinds=("static_clean",),
        )
        self.assertEqual(result["variants"][bad.__name__]["status"], "FAILED")
        self.assertEqual(result["variants"][good.__name__]["status"], "OK")

    def test_cli_canonical_metrics_reproduce_with_separate_vm_timing(self):
        with tempfile.TemporaryDirectory(prefix="sf-json-only-") as folder:
            first_path, second_path = Path(folder) / "first.json", Path(folder) / "second.json"
            common = ["--variant", VARIANT, "--per-kind", "1", "--frames", "3",
                      "--size", "64", "48", "--kind", "static_clean", "--kind", "caret_blink"]
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                self.assertEqual(screen_id_eval.main(common + ["--out", str(first_path)]), 0)
                self.assertEqual(screen_id_eval.main(common + ["--out", str(second_path)]), 0)
            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
            text = first_path.read_text(encoding="utf-8")
            parsed = json.loads(text)
            self.assertEqual(text.rstrip("\n"), json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
            self.assertEqual(parsed["split"], "DEV")
            self.assertIn("VM", stderr.getvalue())
            self.assertNotIn("MEASURED", stderr.getvalue())
            self.assertEqual({p.name for p in Path(folder).iterdir()}, {"first.json", "second.json"})


if __name__ == "__main__":
    unittest.main()
