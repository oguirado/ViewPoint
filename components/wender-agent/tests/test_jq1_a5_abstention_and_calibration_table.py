import unittest
from wender.state import schema

# ---------------------------------------------------------------------------
# Test-local explicit reference implementations for A5-D2 selective risk
# ---------------------------------------------------------------------------

ACTIONABILITY_THRESHOLD = 900
ROLE_MARGIN_THRESHOLD = 50


def compute_smoothed_bin_confidence(tp: int, fp: int) -> int:
    """Computes Laplace-smoothed bin confidence scaled by 1000 using integer floor.

    Formula: floor(1000 * (TP + 1) / (TP + FP + 2))
    """
    return (1000 * (tp + 1)) // (tp + fp + 2)


def is_rational_greater(tp_a: int, fp_a: int, tp_b: int, fp_b: int) -> bool:
    """Compares unrounded rational confidences (TP_a + 1)/(TP_a + FP_a + 2) > (TP_b + 1)/(TP_b + FP_b + 2)

    Using cross-multiplication with integer arithmetic.
    """
    num_a = tp_a + 1
    den_a = tp_a + fp_a + 2
    num_b = tp_b + 1
    den_b = tp_b + fp_b + 2
    return num_a * den_b > num_b * den_a


def calibrate_monotonic_bins(bins):
    """Monotonic bin calibration using Pool Adjacent Violators (PAV).

    Inputs:
        bins: list of (tp, fp) tuples for ordered bins.

    Returns:
        List of dicts per bin containing pooled_tp, pooled_fp, and output confidence integer.
    """
    stack = []  # list of dicts: {"tp": int, "fp": int, "indices": [int]}

    for i, (tp, fp) in enumerate(bins):
        curr = {"tp": tp, "fp": fp, "indices": [i]}
        while stack and is_rational_greater(stack[-1]["tp"], stack[-1]["fp"], curr["tp"], curr["fp"]):
            prev = stack.pop()
            curr["tp"] += prev["tp"]
            curr["fp"] += prev["fp"]
            curr["indices"] = prev["indices"] + curr["indices"]
        stack.append(curr)

    result = [None] * len(bins)
    for pool in stack:
        conf = compute_smoothed_bin_confidence(pool["tp"], pool["fp"])
        for idx in pool["indices"]:
            result[idx] = {
                "bin_index": idx,
                "pooled_tp": pool["tp"],
                "pooled_fp": pool["fp"],
                "confidence": conf,
            }
    return result


def evaluate_abstention_policy(confidence: int, role_margin: int, role: str = "button") -> dict:
    """Evaluates actionability threshold and role-margin threshold."""
    if confidence < ACTIONABILITY_THRESHOLD:
        return {
            "decision": "ABSTAIN",
            "permitted_role": None,
            "confidence": confidence,
            "role_margin": role_margin,
        }
    if role_margin < ROLE_MARGIN_THRESHOLD:
        return {
            "decision": "INTERACTIVE_UNKNOWN",
            "permitted_role": None,
            "confidence": confidence,
            "role_margin": role_margin,
        }
    return {
        "decision": "PERMIT_ROLE",
        "permitted_role": role,
        "confidence": confidence,
        "role_margin": role_margin,
    }


def compute_selective_risk_metrics(predictions, ground_truths) -> dict:
    """Computes class-agnostic recall and role-conditioned precision in ppm.

    Specify zero-denominator metrics as 'UNAVAILABLE' rather than numeric zero.
    """
    total_gt = len(ground_truths)
    total_pred = len(predictions)

    tp_loc = sum(1 for p in predictions if p.get("localized") is True)
    tp_role = sum(1 for p in predictions if p.get("localized") is True and p.get("role_correct") is True)

    if total_gt == 0:
        recall_ppm = "UNAVAILABLE"
    else:
        recall_ppm = (tp_loc * 1000000) // total_gt

    if tp_loc == 0:
        precision_ppm = "UNAVAILABLE"
    else:
        precision_ppm = (tp_role * 1000000) // tp_loc

    return {
        "class_agnostic_recall_ppm": recall_ppm,
        "role_conditioned_precision_ppm": precision_ppm,
        "tp_localized": tp_loc,
        "tp_role": tp_role,
        "total_gt": total_gt,
        "total_pred": total_pred,
    }


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------


class TestA5AbstentionAndCalibrationTable(unittest.TestCase):

    def test_smoothed_bin_confidence_calculations(self):
        # TP 90 and FP 10 must produce 892
        conf_90_10 = compute_smoothed_bin_confidence(90, 10)
        self.assertEqual(conf_90_10, 892)

        # Empty bin (TP 0, FP 0) must produce 500, not zero evidence
        conf_empty = compute_smoothed_bin_confidence(0, 0)
        self.assertEqual(conf_empty, 500)

        # Additional edge cases
        self.assertEqual(compute_smoothed_bin_confidence(100, 0), 990)
        self.assertEqual(compute_smoothed_bin_confidence(0, 100), 9)

    def test_monotonic_calibration_reversed_evidence(self):
        # Bin 0: TP=90, FP=10 (unpooled confidence 892)
        # Bin 1: TP=10, FP=90 (unpooled confidence 107)
        # Reversed order violates monotonicity -> merged to pooled TP=100, FP=100
        bins = [(90, 10), (10, 90)]
        calibrated = calibrate_monotonic_bins(bins)

        self.assertEqual(len(calibrated), 2)
        # Pooled conf = 1000 * (100 + 1) // (100 + 100 + 2) = 101000 // 202 = 500
        self.assertEqual(calibrated[0]["confidence"], 500)
        self.assertEqual(calibrated[1]["confidence"], 500)
        self.assertEqual(calibrated[0]["pooled_tp"], 100)
        self.assertEqual(calibrated[1]["pooled_tp"], 100)

    def test_monotonic_calibration_equal_confidence(self):
        # Equal confidence bins: no strict violation -> maintain separate pooled counts
        bins = [(10, 10), (20, 20)]
        calibrated = calibrate_monotonic_bins(bins)

        self.assertEqual(len(calibrated), 2)
        # UnpooledBin0: 1000 * 11 // 22 = 500
        # UnpooledBin1: 1000 * 21 // 42 = 500
        self.assertEqual(calibrated[0]["confidence"], 500)
        self.assertEqual(calibrated[1]["confidence"], 500)
        self.assertEqual(calibrated[0]["pooled_tp"], 10)
        self.assertEqual(calibrated[1]["pooled_tp"], 20)

    def test_monotonic_calibration_deterministic_repeated_pooling(self):
        # Cascading violation:
        # Bin 0: TP=10, FP=0  (rational 11/12)
        # Bin 1: TP=10, FP=10 (rational 11/22)
        # Bin 2: TP=0,  FP=10 (rational 1/12)
        # Bin 2 violates Bin 1 -> merged (TP=10, FP=20, rational 11/32)
        # Bin 1+2 (11/32) violates Bin 0 (11/12) -> merged all (TP=20, FP=20, rational 21/42)
        bins = [(10, 0), (10, 10), (0, 10)]
        calibrated = calibrate_monotonic_bins(bins)

        self.assertEqual(len(calibrated), 3)
        self.assertEqual(calibrated[0]["confidence"], 500)
        self.assertEqual(calibrated[1]["confidence"], 500)
        self.assertEqual(calibrated[2]["confidence"], 500)
        for b in calibrated:
            self.assertEqual(b["pooled_tp"], 20)
            self.assertEqual(b["pooled_fp"], 20)

    def test_abstention_policy_decisions(self):
        # Confidence 200 emits ABSTAIN
        res_abstain = evaluate_abstention_policy(confidence=200, role_margin=100)
        self.assertEqual(res_abstain["decision"], "ABSTAIN")
        self.assertIsNone(res_abstain["permitted_role"])

        # Confidence 920 with margin 15 emits INTERACTIVE_UNKNOWN
        res_unknown = evaluate_abstention_policy(confidence=920, role_margin=15)
        self.assertEqual(res_unknown["decision"], "INTERACTIVE_UNKNOWN")
        self.assertIsNone(res_unknown["permitted_role"])

        # Confidence 920 with margin 60 permits a specific role
        res_permit = evaluate_abstention_policy(confidence=920, role_margin=60, role="button")
        self.assertEqual(res_permit["decision"], "PERMIT_ROLE")
        self.assertEqual(res_permit["permitted_role"], "button")

    def test_selective_risk_metrics_ppm(self):
        # Ten localized predictions with five wrong roles
        ground_truths = [{"id": i} for i in range(10)]
        predictions = [
            {"id": i, "localized": True, "role_correct": (i < 5)}
            for i in range(10)
        ]

        metrics = compute_selective_risk_metrics(predictions, ground_truths)
        self.assertEqual(metrics["class_agnostic_recall_ppm"], 1000000)
        self.assertEqual(metrics["role_conditioned_precision_ppm"], 500000)
        self.assertEqual(metrics["tp_localized"], 10)
        self.assertEqual(metrics["tp_role"], 5)

    def test_selective_risk_metrics_zero_denominator_unavailable(self):
        # Zero ground truths -> recall unavailable
        metrics_no_gt = compute_selective_risk_metrics(
            predictions=[{"id": 0, "localized": True, "role_correct": True}],
            ground_truths=[]
        )
        self.assertEqual(metrics_no_gt["class_agnostic_recall_ppm"], "UNAVAILABLE")
        self.assertEqual(metrics_no_gt["role_conditioned_precision_ppm"], 1000000)

        # Zero localized predictions -> precision unavailable
        metrics_no_loc = compute_selective_risk_metrics(
            predictions=[{"id": 0, "localized": False, "role_correct": False}],
            ground_truths=[{"id": 0}]
        )
        self.assertEqual(metrics_no_loc["class_agnostic_recall_ppm"], 0)
        self.assertEqual(metrics_no_loc["role_conditioned_precision_ppm"], "UNAVAILABLE")

    def test_schema_dumps_accepts_results_and_rejects_floats(self):
        # Verify schema.dumps accepts reference output structures
        calibration = calibrate_monotonic_bins([(90, 10), (10, 90)])
        policy_res = evaluate_abstention_policy(920, 60, "button")
        metrics = compute_selective_risk_metrics(
            predictions=[{"id": i, "localized": True, "role_correct": (i < 5)} for i in range(10)],
            ground_truths=[{"id": i} for i in range(10)]
        )

        reference_results = {
            "calibration": calibration,
            "policy": policy_res,
            "metrics": metrics,
            "thresholds": {
                "actionability": ACTIONABILITY_THRESHOLD,
                "role_margin": ROLE_MARGIN_THRESHOLD,
            }
        }

        # schema.dumps must succeed without error
        dumped = schema.dumps(reference_results)
        self.assertIsInstance(dumped, str)

        # schema.dumps must raise StateError if floats are present
        bad_payload = {"score": 0.892}
        with self.assertRaises(schema.StateError):
            schema.dumps(bad_payload)


if __name__ == "__main__":
    unittest.main()
