"""Adversarial regression test for DEF-A2-01: positive-claim-only FL chain veto gap."""
import unittest
from unittest import mock
import numpy as np

from eval.v2eval.chain import _ClaimVariant, annotate_flicker_fp, compose_chain, run_chain
from eval.v2eval.plugins import Variant
from eval.v2eval.postpass import BaselineFlickerVeto, PostPass, apply_claims
from eval.v2proto_flicker import FlickerTracker
from visioncore import desc
from visioncore.grid import Grid


class TestJQ1A2ChainClaimOnlyVetoGap(unittest.TestCase):
    def setUp(self):
        # Four-tile setup representing:
        # tile 0: truth CHANGED scroll-exposed (baseline false FLICKER)
        # tile 1: truth CHANGED canvas (baseline false FLICKER)
        # tile 2: GM-claimed MOVED interior (baseline CHANGED)
        # tile 3: UNCHANGED
        self.baseline = {
            "cls": np.array([4, 4, 2, 0], dtype=np.int64),
            "mv_dx": np.array([0, 0, 0, 0], dtype=np.int64),
            "mv_dy": np.array([0, 0, 0, 0], dtype=np.int64),
        }

        # GM output claims tile 2 as MOVED (class 3)
        self.out_gm = {
            "cls": np.array([4, 4, 3, 0], dtype=np.int64),
            "mv_dx": np.array([0, 0, 5, 0], dtype=np.int64),
            "mv_dy": np.array([0, 0, 0, 0], dtype=np.int64),
        }

        # FlickerTracker fed with 4 distinct tile hashes emits no class-4 claims
        self.tracker = FlickerTracker(4)
        hashes = np.array([0x1111, 0x2222, 0x3333, 0x4444], dtype=np.uint64)
        states, _ = self.tracker.update(hashes)
        fl_mask = states == 4  # All False
        self.fl_claim = {
            "mask": fl_mask,
            "cls": 4,
            "mv_dx": np.zeros(4, dtype=np.int64),
            "mv_dy": np.zeros(4, dtype=np.int64),
        }

        # FL output through apply_claims as the chain does
        self.out_fl = apply_claims(self.baseline, [self.fl_claim])

        # Reproduce run_chain difference masks and gm_then_fl precedence
        self.gm_mask = self.out_gm["cls"] != self.baseline["cls"]
        self.fl_mask_chain = self.out_fl["cls"] != self.baseline["cls"]

        self.gm_claim = {
            "mask": self.gm_mask,
            "cls": 3,
            "mv_dx": self.out_gm["mv_dx"],
            "mv_dy": self.out_gm["mv_dy"],
        }
        self.fl_claim_eff = {
            "mask": self.fl_mask_chain & (~self.gm_mask),
            "cls": 4,
            "mv_dx": self.out_fl["mv_dx"],
            "mv_dy": self.out_fl["mv_dy"],
        }
        self.legacy_out_chain = apply_claims(self.baseline, [self.gm_claim, self.fl_claim_eff])
        # This is an explicit rejection-channel fixture, not a claim that
        # tracker abstention proves nonblink. Production pixel evidence is
        # exercised separately below. The existing class assertions are kept.
        self.veto = np.array([True, True, False, False], dtype=np.bool_)
        self.out_chain = compose_chain(self.baseline, self.gm_claim, self.fl_claim,
                                       flicker_veto=self.veto)

    def test_positive_claim_only_limitation_and_gm_winner_preservation(self):
        """Omitting the new negative channel retains positive-only callers."""
        np.testing.assert_array_equal(self.legacy_out_chain["cls"], [4, 4, 3, 0])
        # Preserves GM winner at tile 2 (class 3)
        self.assertEqual(self.out_chain["cls"][2], 3)
        # Preserves unchanged at tile 3 (class 0)
        self.assertEqual(self.out_chain["cls"][3], 0)

    def test_annotate_flicker_fp(self):
        """Test annotate_flicker_fp with small integer confusion matrices."""
        # Test 1: All true-positive case
        c_tp = np.zeros((7, 7), dtype=int)
        c_tp[4, 4] = 10  # 10 true FLICKER predictions
        metrics_tp = {"confusion": c_tp.tolist()}
        res_tp = annotate_flicker_fp(metrics_tp)
        self.assertEqual(res_tp["flicker_false_positives"], 0)
        self.assertEqual(res_tp["flicker_fp"], 0)

        # Test 2: Mixture of CHANGED-to-FLICKER errors and true FLICKER at aggregate & per-kind levels
        c_mix = np.zeros((7, 7), dtype=int)
        c_mix[4, 4] = 7  # 7 true positives
        c_mix[2, 4] = 4  # 4 CHANGED-to-FLICKER false positives
        c_mix[0, 4] = 1  # 1 UNCHANGED-to-FLICKER false positive
        # Column 4 total = 12, diagonal = 7, false positives = 12 - 7 = 5

        kc1 = np.zeros((7, 7), dtype=int)
        kc1[4, 4] = 5
        kc1[2, 4] = 3  # Column 4 total = 8, diagonal = 5, fp = 3

        kc2 = np.zeros((7, 7), dtype=int)
        kc2[4, 4] = 2
        kc2[0, 4] = 2  # Column 4 total = 4, diagonal = 2, fp = 2

        metrics_mix = {
            "confusion": c_mix.tolist(),
            "per_kind": {
                "scroll_exposed": {"confusion": kc1.tolist()},
                "canvas": {"confusion": kc2.tolist()},
            },
        }

        res_mix = annotate_flicker_fp(metrics_mix)

        # Aggregate assertions
        expected_agg_fp = int(c_mix[:, 4].sum() - c_mix[4, 4])
        self.assertEqual(expected_agg_fp, 5)
        self.assertEqual(res_mix["flicker_false_positives"], expected_agg_fp)
        self.assertEqual(res_mix["flicker_fp"], expected_agg_fp)

        # Per-kind assertions
        k1_fp = int(kc1[:, 4].sum() - kc1[4, 4])
        self.assertEqual(k1_fp, 3)
        self.assertEqual(res_mix["per_kind"]["scroll_exposed"]["flicker_false_positives"], k1_fp)
        self.assertEqual(res_mix["per_kind"]["scroll_exposed"]["flicker_fp"], k1_fp)

        k2_fp = int(kc2[:, 4].sum() - kc2[4, 4])
        self.assertEqual(k2_fp, 2)
        self.assertEqual(res_mix["per_kind"]["canvas"]["flicker_false_positives"], k2_fp)
        self.assertEqual(res_mix["per_kind"]["canvas"]["flicker_fp"], k2_fp)

    def test_def_a2_01_desired_veto_contract(self):
        """DEF-A2-01: desired veto contract demotes rejected baseline FLICKER tiles to CHANGED.

        Explicit independent rejection of baseline tiles 0 & 1 demotes them to
        class 2 while retaining GM winner 2 and unchanged tile 3. Absence of a
        positive tracker claim alone continues to preserve baseline labels.
        """
        desired_cls = np.array([2, 2, 3, 0], dtype=np.int64)
        np.testing.assert_array_equal(self.out_chain["cls"], desired_cls)

    def test_veto_restriction_positive_overlap_precedence_and_immutability(self):
        baseline = {k: v.copy() for k, v in self.baseline.items()}
        gm = {k: v.copy() if isinstance(v, np.ndarray) else v
              for k, v in self.gm_claim.items()}
        fl = {k: v.copy() if isinstance(v, np.ndarray) else v
              for k, v in self.fl_claim.items()}
        # A genuine positive FL claim whose class equals baseline must survive
        # the veto. The same tile is also a GM claim to exercise named priority.
        gm["mask"][0] = True
        gm["mv_dx"][0] = 9
        fl["mask"][:2] = True
        veto = np.ones(4, dtype=np.bool_)
        originals = [a.copy() for obj in (baseline, gm, fl) for a in obj.values()
                     if isinstance(a, np.ndarray)]
        for obj in (baseline, gm, fl):
            for value in obj.values():
                if isinstance(value, np.ndarray):
                    value.flags.writeable = False
        veto.flags.writeable = False
        first_gm = compose_chain(baseline, gm, fl, "gm_then_fl", flicker_veto=veto)
        first_fl = compose_chain(baseline, gm, fl, "fl_then_gm", flicker_veto=veto)
        np.testing.assert_array_equal(first_gm["cls"], [3, 4, 3, 0])
        np.testing.assert_array_equal(first_fl["cls"], [4, 4, 3, 0])
        self.assertEqual(first_gm["mv_dx"][0], 9)
        self.assertEqual(first_fl["mv_dx"][0], 0)
        bare = apply_claims(baseline, [], flicker_veto=veto)
        np.testing.assert_array_equal(bare["cls"], [2, 2, 2, 0])
        for key in ("mv_dx", "mv_dy"):
            np.testing.assert_array_equal(bare[key], baseline[key])
        after = [a for obj in (baseline, gm, fl) for a in obj.values()
                 if isinstance(a, np.ndarray)]
        for before, value in zip(originals, after):
            np.testing.assert_array_equal(value, before)
        np.testing.assert_array_equal(veto, np.ones(4, dtype=np.bool_))

    def test_explicit_veto_rejects_integer_and_broadcast_masks(self):
        for invalid in ([True] * 4, np.ones(4, dtype=np.uint8),
                        np.ones((4, 1), dtype=np.bool_), np.ones(1, dtype=np.bool_)):
            with self.subTest(shape=np.shape(invalid)):
                with self.assertRaises(ValueError):
                    apply_claims(self.baseline, [], flicker_veto=invalid)
        grid = Grid(16, 16)
        rgb = np.zeros((16, 16, 3), dtype=np.uint8)
        tiles, _ = desc.describe_padded(grid.pad(rgb), grid)
        post = PostPass(grid, accelerated=False, baseline_flicker_veto=True)
        with self.assertRaises(ValueError):
            post.update(tiles, rgb, flicker_veto=np.ones(1, dtype=np.uint8))
        self.assertFalse(post.tracker.initialized)
        self.assertIsNone(post.veto.last_frame_id)
        self.assertEqual(post.veto.history, [])

    def test_worker_claim_capture_retains_baseline_equal_positive_claim(self):
        claim = {"mask": np.array([True, False, False, False]), "cls": 4,
                 "mv_dx": np.zeros(4, dtype=np.int64),
                 "mv_dy": np.zeros(4, dtype=np.int64)}
        adapter = object.__new__(_ClaimVariant)
        payload = (claim, 17, 0)
        with mock.patch.object(Variant, "_receive", return_value=payload):
            self.assertIs(adapter._receive(), payload)
        self.assertIs(adapter.last_claim, claim)
        result = compose_chain(self.baseline, self.gm_claim, adapter.last_claim,
                               flicker_veto=self.veto)
        np.testing.assert_array_equal(result["cls"], [4, 2, 3, 0])

    def test_real_chain_production_veto_and_cost_scope(self):
        from eval.gui_synth.scenes import build

        scene = build("canvas_anim", 1, w=160, h=96, n=12, split="DEV")
        result = run_chain([scene], "gm=tests.test_v2eval.toy_variants",
                           "fl=eval.v2x.fl_v3")
        self.assertTrue(result["baseline_flicker_veto"]["enabled"])
        variants = result["variants"]
        self.assertGreater(variants["baseline"]["metrics"]["flicker_fp"], 0)
        for key in ("fl=eval.v2x.fl_v3", "gm_then_fl"):
            self.assertEqual(variants[key]["status"], "OK", variants[key]["error"])
            self.assertEqual(variants[key]["metrics"]["flicker_fp"], 0)
            self.assertIsNone(variants[key]["python_opcodes"])

    def test_eager_adapter_stale_hashes_spurious_proposal_and_buffer_ownership(self):
        from eval.gui_synth import raster
        from eval.v2x.fl_v3 import Reason, veto_flicker
        from tests.test_jq1_a2_s2_flicker_veto import _records, _reference_params

        grid = Grid(160, 96)
        scene = {"kind": "canvas_anim", "w": 160, "h": 96, "seed": 1,
                 "boxes": [[0, 0, 160, 96]], "glyph_runs": [], "scale": 1}
        frames = [raster.frame(scene, i) for i in range(12)]
        records = _records(frames, grid)
        adapter = BaselineFlickerVeto(grid)
        reusable = np.empty_like(frames[0])
        count = 0
        for i, (rgb, record) in enumerate(zip(frames, records)):
            reusable[:] = rgb
            stale = record["s2"].copy()
            stale["xh64"] = 0
            before = stale.copy()
            # An unsupported internal motion proposal supplies no F6 evidence;
            # it must not invalidate P2 established without asserted GM.
            with mock.patch("eval.v2eval.postpass.change.global_vector", return_value=(17, 0)):
                result = adapter.update(stale, reusable, frame_id=i)
            expected = veto_flicker(
                record["s2"], records[i-2]["raw"] if i >= 2 else None,
                records[i-1]["raw"] if i >= 1 else None, record["equal"],
                (grid.s, grid.gw, grid.gh),
                y_prev2=records[i-2]["y"] if i >= 2 else None,
                y_prev1=records[i-1]["y"] if i >= 1 else None, y_cur=record["y"],
                params=_reference_params(grid.N, i))
            np.testing.assert_array_equal(result.mask, expected.mask)
            np.testing.assert_array_equal(adapter.current_tiles["xh64"], record["raw"]["xh64"])
            np.testing.assert_array_equal(stale, before)
            reusable.fill(0)
            np.testing.assert_array_equal(adapter.history[-1][2], record["y"])
            count += int(result.mask.sum())
        self.assertGreater(count, 0)
        with mock.patch("eval.v2eval.postpass.change.global_vector", return_value=(17, 0)):
            gap = adapter.update(records[-1]["s2"], frames[-1], frame_id=99)
        self.assertTrue(adapter.discontinuous)
        self.assertFalse(gap.mask.any())
        candidates = records[-1]["s2"]["cls"] == 4
        self.assertTrue(np.all(gap.reasons[candidates] == Reason.MISSING_HISTORY))
        adapter.reset()
        self.assertIsNone(adapter.current_tiles)
        self.assertEqual(adapter.history, [])

    def test_postpass_fresh_hashes_preserve_true_tracker_claim_after_veto(self):
        grid = Grid(16, 16)
        a = np.zeros((16, 16, 3), dtype=np.uint8)
        b = a.copy()
        b[4:6, 4:12] = 250
        post = PostPass(grid, accelerated=False, baseline_flicker_veto=True)
        declarations = []
        with mock.patch("eval.v2eval.postpass.estimate_global_motion", return_value=None), \
                mock.patch("eval.v2eval.postpass.subtile_motion", return_value=None):
            for i in range(32):
                rgb = a if (i // 8) % 2 == 0 else b
                tiles, _ = desc.describe_padded(grid.pad(rgb), grid)
                tiles["cls"] = 4  # Protocol fixture: candidate and positive overlap.
                tiles["xh64"] = 0  # Emitted hashes are deliberately untrusted.
                output, claims = post.update(tiles, rgb, frame_id=i,
                                            flicker_veto=np.ones(1, dtype=np.bool_))
                if output["flicker"]["cls"][0] == 4:
                    declarations.append(i)
                self.assertEqual(output["combined"]["cls"][0], output["flicker"]["cls"][0])
                self.assertTrue(claims["flicker"]["veto_mask"][0])
                self.assertEqual(int(tiles["xh64"][0]), 0)
        self.assertEqual(declarations, [24])
        with mock.patch("eval.v2eval.postpass.estimate_global_motion", return_value=None), \
                mock.patch("eval.v2eval.postpass.subtile_motion", return_value=None):
            output, _ = post.update(tiles, a, frame_id=99,
                                    flicker_veto=np.ones(1, dtype=np.bool_))
        self.assertFalse(post.tracker.in_flicker.any())
        self.assertEqual(len(post.veto.history), 1)
        self.assertEqual(output["flicker"]["cls"][0], 2)
        post.reset()
        self.assertFalse(post.tracker.initialized)
        self.assertIsNone(post.prev)
        self.assertEqual(post.veto.history, [])

    def test_postpass_combined_order_legacy_and_opt_in(self):
        grid = Grid(16, 16)
        a = np.zeros((16, 16, 3), dtype=np.uint8)
        b = np.full_like(a, 200)
        tiles, _ = desc.describe_padded(grid.pad(a), grid)
        tiles["cls"] = 4
        for enabled in (False, True):
            with self.subTest(baseline_flicker_veto=enabled):
                post = PostPass(grid, accelerated=False, baseline_flicker_veto=enabled)
                post.update(tiles, a)
                with mock.patch.object(post.tracker, "update", return_value=(np.array([4]), np.array([8]))), \
                        mock.patch("eval.v2eval.postpass.estimate_global_motion", return_value=(1, 0, 0)), \
                        mock.patch("eval.v2eval.postpass.consensus_tiles", return_value=np.ones((1, 1), dtype=np.bool_)):
                    output, _ = post.update(tiles, b, flicker_veto=np.ones(1, dtype=np.bool_))
                self.assertEqual(output["global_motion"]["cls"][0], 3)
                self.assertEqual(output["combined"]["cls"][0], 3 if enabled else 4)
                self.assertEqual(output["combined"]["mv_dx"][0], 1 if enabled else 0)


if __name__ == "__main__":
    unittest.main()
