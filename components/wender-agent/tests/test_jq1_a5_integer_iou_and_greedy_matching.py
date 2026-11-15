"""Reference integer IoU evaluation and live integration-gap regression for A5-D2.

This test module defines test-local pure helpers (explicitly reference-only because production
evaluation APIs are absent) using half-open integer x,y,w,h bounding boxes and cross-multiplication
threshold checks.
"""
import itertools
import unittest

from wender.perceive.vision_adapter import fuse_regions
from wender.state import schema


def compute_integer_iou(box_p, box_g):
    """Compute exact integer intersection area and union area for half-open [x, y, w, h] boxes.

    Returns (inter, union).
    Raises ValueError if union is 0.
    """
    px, py, pw, ph = box_p
    gx, gy, gw, gh = box_g

    if pw < 0 or ph < 0 or gw < 0 or gh < 0:
        raise ValueError("Box dimensions must be non-negative")

    ix0 = max(px, gx)
    iy0 = max(py, gy)
    ix1 = min(px + pw, gx + gw)
    iy1 = min(py + ph, gy + gh)

    if ix1 > ix0 and iy1 > iy0:
        inter = (ix1 - ix0) * (iy1 - iy0)
    else:
        inter = 0

    area_p = pw * ph
    area_g = gw * gh
    union = area_p + area_g - inter

    if union <= 0:
        raise ValueError("Union must be strictly positive")

    return inter, union


def satisfies_iou_threshold(inter, union, threshold_num=1, threshold_den=2):
    """Check if inter / union >= threshold_num / threshold_den via cross-multiplication."""
    if union <= 0:
        raise ValueError("Union must be strictly positive")
    return threshold_num * union <= threshold_den * inter


def greedy_match_predictions(predictions, ground_truths, threshold_num=1, threshold_den=2):
    """Greedy matching of predictions against ground truths using integer IoU cross-multiplication.

    Predictions are sorted by:
      - descending score_q1000
      - ascending tie-breaker tuple (role, x, y, w, h, id)

    Ground truths are visited by:
      - descending integer IoU (evaluated via cross-multiplication)
      - ascending identifier

    Returns a dict with matches, false_positives, and false_negatives.
    """
    sorted_preds = sorted(
        predictions,
        key=lambda p: (
            -p["score_q1000"],
            p["role"],
            p["x"],
            p["y"],
            p["w"],
            p["h"],
            p["id"],
        ),
    )

    unmatched_gt_ids = set(g["id"] for g in ground_truths)
    gt_map = {g["id"]: g for g in ground_truths}

    matches = []
    false_positives = []

    for pred in sorted_preds:
        pred_box = (pred["x"], pred["y"], pred["w"], pred["h"])
        candidate_matches = []

        for gt_id in list(unmatched_gt_ids):
            gt = gt_map[gt_id]
            gt_box = (gt["x"], gt["y"], gt["w"], gt["h"])
            inter, union = compute_integer_iou(pred_box, gt_box)

            if satisfies_iou_threshold(inter, union, threshold_num, threshold_den):
                candidate_matches.append({
                    "gt_id": gt_id,
                    "inter": inter,
                    "union": union,
                })

        if candidate_matches:
            # Sort candidate ground truths:
            # Primary: descending IoU. For two candidates A and B, IoU_A > IoU_B iff inter_A * union_B > inter_B * union_A.
            # Secondary: ascending gt_id.
            def cmp_key(cand):
                # To sort descending by IoU using integer tuple:
                # We can construct a key or use custom sort.
                return cand

            # Use a custom comparator key for ground truths based on cross-multiplication comparison
            class IoUKer:
                def __init__(self, cand):
                    self.cand = cand

                def __lt__(self, other):
                    # Higher IoU comes first.
                    # self < other means self has HIGHER priority than other.
                    lhs = self.cand["inter"] * other.cand["union"]
                    rhs = other.cand["inter"] * self.cand["union"]
                    if lhs != rhs:
                        return lhs > rhs  # higher IoU comes first
                    return self.cand["gt_id"] < other.cand["gt_id"]

            candidate_matches.sort(key=IoUKer)
            best_gt = candidate_matches[0]
            matched_gt_id = best_gt["gt_id"]
            unmatched_gt_ids.remove(matched_gt_id)

            matches.append({
                "pred_id": pred["id"],
                "gt_id": matched_gt_id,
                "inter": best_gt["inter"],
                "union": best_gt["union"],
            })
        else:
            false_positives.append({
                "pred_id": pred["id"],
            })

    false_negatives = [{"gt_id": gt_id} for gt_id in sorted(unmatched_gt_ids)]

    return {
        "matches": matches,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
    }


class TestIntegerIoUAndGreedyMatching(unittest.TestCase):
    def test_integer_iou_calculation_and_thresholds(self):
        # Ground truth: x=0, y=0, w=6, h=6 (area 36)
        gt_box = [0, 0, 6, 6]

        # Prediction 1: x=2, y=0, w=6, h=6 (area 36) -> inter=24, union=48
        pred1_box = [2, 0, 6, 6]
        inter1, union1 = compute_integer_iou(pred1_box, gt_box)
        self.assertEqual(inter1, 24)
        self.assertEqual(union1, 48)
        # Exactly IoU 1/2: 2 * 24 = 48 >= 48 (passes)
        self.assertTrue(satisfies_iou_threshold(inter1, union1, threshold_num=1, threshold_den=2))

        # Prediction 2: shift prediction x to 3 -> x=3, y=0, w=6, h=6 -> inter=18, union=54
        pred2_box = [3, 0, 6, 6]
        inter2, union2 = compute_integer_iou(pred2_box, gt_box)
        self.assertEqual(inter2, 18)
        self.assertEqual(union2, 54)
        # Strict miss at 1/2 threshold: 2 * 18 = 36 < 54
        self.assertFalse(satisfies_iou_threshold(inter2, union2, threshold_num=1, threshold_den=2))

        # Test three-quarters threshold (4 * inter >= 3 * union)
        # Box with IoU = 3/4
        # GT: [0, 0, 4, 4] (area 16), Pred: [1, 0, 4, 4] (area 16) -> inter = 3*4 = 12, union = 16+16-12 = 20.
        # 4 * 12 = 48 >= 3 * 20 = 60 ? False (12/20 = 0.6 < 0.75)
        # For IoU 3/4: GT [0, 0, 8, 8] (64), Pred [2, 0, 8, 8] (64) -> inter = 6*8 = 48, union = 64+64-48 = 80.
        # 4 * 48 = 192 >= 3 * 80 = 240 ? False (48/80 = 0.6)
        # GT [0, 0, 8, 8] (64), Pred [1, 0, 8, 8] (64) -> inter = 7*8 = 56, union = 64+64-56 = 72.
        # 4 * 56 = 224 >= 3 * 72 = 216 -> True! (56/72 = 7/9 = 0.777... >= 0.75)
        inter3, union3 = compute_integer_iou([1, 0, 8, 8], [0, 0, 8, 8])
        self.assertEqual((inter3, union3), (56, 72))
        self.assertTrue(satisfies_iou_threshold(inter3, union3, threshold_num=3, threshold_den=4))

        # Test zero union rejection
        with self.assertRaises(ValueError):
            compute_integer_iou([0, 0, 0, 0], [0, 0, 0, 0])

    def test_greedy_matching_two_identical_predictions_permutation_invariance(self):
        gt = [{"id": "gt_01", "role": "button", "x": 0, "y": 0, "w": 10, "h": 10}]
        p1 = {"id": "pred_01", "role": "button", "x": 0, "y": 0, "w": 10, "h": 10, "score_q1000": 900}
        p2 = {"id": "pred_02", "role": "button", "x": 0, "y": 0, "w": 10, "h": 10, "score_q1000": 900}

        # Test both permutations of predictions input
        res1 = greedy_match_predictions([p1, p2], gt)
        res2 = greedy_match_predictions([p2, p1], gt)

        self.assertEqual(len(res1["matches"]), 1)
        self.assertEqual(len(res1["false_positives"]), 1)
        self.assertEqual(len(res1["false_negatives"]), 0)

        self.assertEqual(len(res2["matches"]), 1)
        self.assertEqual(len(res2["false_positives"]), 1)
        self.assertEqual(len(res2["false_negatives"]), 0)

        # Confirm exact output structure and tie-breaker outcome (pred_01 comes before pred_02 alphabetically)
        self.assertEqual(res1["matches"][0]["pred_id"], "pred_01")
        self.assertEqual(res1["false_positives"][0]["pred_id"], "pred_02")
        self.assertEqual(res2["matches"][0]["pred_id"], "pred_01")
        self.assertEqual(res2["false_positives"][0]["pred_id"], "pred_02")

        # Verify serialization with schema.dumps
        dumped1 = schema.dumps(res1)
        dumped2 = schema.dumps(res2)
        self.assertEqual(dumped1, dumped2)
        self.assertNotIn(".", dumped1)

    def test_fuse_regions_distinction_from_reference_iou_evaluator(self):
        # Node: x=10, y=10, w=20, h=20
        # Region: x=29, y=29, w=20, h=20
        # Intersection: inter = 1, union = 20*20 + 20*20 - 1 = 799
        node_box = [10, 10, 20, 20]
        region_box = [29, 29, 20, 20]

        inter, union = compute_integer_iou(node_box, region_box)
        self.assertEqual(inter, 1)
        self.assertEqual(union, 799)

        # Reference IoU evaluator rejects this low IoU (1/799)
        self.assertFalse(satisfies_iou_threshold(inter, union, threshold_num=1, threshold_den=2))

        # Ordinary assertion pins that current fuse_regions reports overlap = 1
        pruned_nodes = [
            {"id": "0000000000000001", "role": "button", "name": "Target", "depth": 0, "ordinal": 0, "attrs": ()},
        ]
        boxes = {"0000000000000001": node_box}
        state = schema.build_state(
            pruned_nodes,
            kind="cdp",
            url="https://example.test",
            title="Test",
            ax_sha256="a" * 64,
            ax_stable_sha256="b" * 64,
            prune={"name": "prune_ax_v1", "version": 1},
            raw_count=1,
            boxes=boxes,
            actionable_roles={"button"},
        )

        regions = [{"x": 29, "y": 29, "w": 20, "h": 20, "tiles": 1}]
        fused = fuse_regions(state, regions, dpr_ppm=1000000)

        self.assertEqual(len(fused["hits"]), 1)
        self.assertEqual(fused["hits"][0]["overlap"], 1)

    def test_a5_d2_future_integration_gap_low_iou_exclusion(self):
        """Explicit one-half IoU filtering closes A5-D2 without changing legacy overlap."""
        node_box = [10, 10, 20, 20]
        pruned_nodes = [
            {"id": "0000000000000001", "role": "button", "name": "Target", "depth": 0, "ordinal": 0, "attrs": ()},
        ]
        boxes = {"0000000000000001": node_box}
        state = schema.build_state(
            pruned_nodes,
            kind="cdp",
            url="https://example.test",
            title="Test",
            ax_sha256="a" * 64,
            ax_stable_sha256="b" * 64,
            prune={"name": "prune_ax_v1", "version": 1},
            raw_count=1,
            boxes=boxes,
            actionable_roles={"button"},
        )

        regions = [{"x": 29, "y": 29, "w": 20, "h": 20, "tiles": 1}]
        fused = fuse_regions(state, regions, dpr_ppm=1000000, iou_num=1, iou_den=2)

        # The opt-in candidate set excludes the existing 1/799 overlap control.
        self.assertEqual(len(fused["hits"]), 0)

    def test_fusion_iou_parameters_reject_incomplete_noninteger_and_invalid_ratios(self):
        class IntegerSubclass(int):
            pass

        state = {"nodes": []}
        for terms in (
            {"iou_num": 1}, {"iou_den": 2},
            {"iou_num": None}, {"iou_den": None},
            {"iou_num": None, "iou_den": None},
            {"iou_num": None, "iou_den": 2},
            {"iou_num": 1, "iou_den": None},
            {"iou_num": True, "iou_den": 2},
            {"iou_num": 1, "iou_den": True},
            {"iou_num": IntegerSubclass(1), "iou_den": 2},
            {"iou_num": 1, "iou_den": IntegerSubclass(2)},
            {"iou_num": 1.0, "iou_den": 2},
            {"iou_num": 1, "iou_den": 2.0},
            {"iou_num": "1", "iou_den": 2},
            {"iou_num": -1, "iou_den": 2},
            {"iou_num": 1, "iou_den": 0},
            {"iou_num": 0, "iou_den": -1},
            {"iou_num": 3, "iou_den": 2},
        ):
            with self.subTest(terms=terms), self.assertRaises(ValueError):
                fuse_regions(state, [], **terms)

    def test_fusion_iou_exact_boundary_large_products_and_zero_threshold(self):
        # Python integer products far exceed signed 64-bit and IEEE-754 precision.
        side = 1 << 80
        state = {"nodes": [{"id": "n", "box": [0, 0, side, side], "actionable": True}]}
        regions = [
            {"x": 0, "y": 0, "w": 2 * side, "h": side},
            {"x": side, "y": 0, "w": side, "h": side},  # touching only
            {"x": 0, "y": 0, "w": 0, "h": side},      # empty
        ]
        half = fuse_regions(state, regions, iou_num=1, iou_den=2)
        self.assertEqual(half, {
            "hits": [{"region": 0, "node": "n", "overlap": side * side, "actionable": True}],
            "unmatched_regions": [1, 2],
        })
        self.assertEqual(fuse_regions(state, regions, iou_num=0, iou_den=1), half)
        self.assertEqual(fuse_regions(state, regions), half)
        self.assertEqual(fuse_regions(state, regions, iou_num=side + 1, iou_den=2 * side), {
            "hits": [], "unmatched_regions": [0, 1, 2],
        })

    def test_fusion_threshold_preserves_order_actionability_and_endpoint_composition(self):
        nodes = [
            {"id": "a", "box": [12, 0, 1, 4], "actionable": False},
            {"id": "c", "box": [12, 0, 1, 4], "actionable": True},
            {"id": "b", "box": [12, 0, 1, 4], "actionable": True},
            {"id": "tiny", "box": [12, 0, 1, 1], "actionable": True},
        ]
        state = {"nodes": nodes}
        regions = [{"x": 16, "y": 0, "w": 1, "h": 5}]
        self.assertEqual(fuse_regions(state, regions, 1250000, iou_num=1, iou_den=2), {
            "hits": [
                {"region": 0, "node": "b", "overlap": 4, "actionable": True},
                {"region": 0, "node": "c", "overlap": 4, "actionable": True},
                {"region": 0, "node": "a", "overlap": 4, "actionable": False},
            ],
            "unmatched_regions": [],
        })
        self.assertEqual(len(fuse_regions(state, regions, 1250000)["hits"]), 4)


if __name__ == "__main__":
    unittest.main()
