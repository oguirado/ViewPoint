from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from perf_observation import ARMS, CPUS, LIMITS, VIEWPORT, compare, observations


def receipt(index, work_ns):
    start = 1000 * (index + 1)

    def host(offset):
        sample = {
            "mono_ns": start + offset, "loadavg": [1, 1, 1],
            "mem_available_kib": 30 << 20, "cpu_perf_limited": {"16": 0},
            "processor_cooling_state": {"cooling_device0": 0},
            "temperature_millicelsius": {"thermal_zone0": 50000},
            "cpu_ticks": {f"cpu{cpu}": [offset, 0, 0, offset * 10, 0, 0, 0, 0]
                          for cpu in range(16)},
        }
        return sample

    rows = []
    for row_index, stratum in enumerate(("cold", "warmup", "warm")):
        submit = start + row_index * 100
        end = submit + work_ns + 1
        rows.append({
            "stratum": stratum, "fixture_index": row_index % 2,
            "frame_sha256": "ab"[row_index % 2] * 64, "protected_start_ns": submit + 1,
            "protected_end_ns": end, "host": host(row_index * 100 + 99),
            "timing": {"t_submit": submit, "t_admitted": end + 1,
                       "t_first_byte": end + 2, "t_complete": end + 3},
            "stage_calls": {
                "privacy_sentinel": 3, "privacy_metadata_read": 6, "privacy_footprint": 8,
                "cdp_screenshot": 1, "cdp_ax": 1, "png_validation_and_pixel_digest": 2,
                "frame_sha256": 3, "protected_frame_contract": 1, "semantic_reply_contracts": 1,
            },
            "stage_ns": {"privacy_sentinel": 5},
        })
    arm = ARMS[index]
    return {
        "rc": 0, "stable_inputs": True, "clock": "CLOCK_MONOTONIC", "cpu_affinity": CPUS,
        "limits_bytes": LIMITS, "profile": VIEWPORT, "focus": {"continuous": True},
        "owned_identity_verified": True,
        "privacy_denial": {"acquisitions": 0, "publications": 0, "code": "HUMAN_NEEDED"},
        "before": host(0), "after": host(300), "operations": rows, "warmups": 1,
        "samples": 1, "base_commit": "base", "benchmark_sha256": "benchmark",
        "source_sha256": arm, "arm": arm, "fixture_pins": {"timer": "fixture"},
        "workload_sha256": "workload", "inputs": {"driver": "pinned"},
    }


class ProtectedObservationScoreTests(unittest.TestCase):
    def setUp(self):
        self.reports = [receipt(index, ns) for index, ns in enumerate((80, 70, 70, 80))]

    def test_paired_p95_oracle_requires_both_improvements(self):
        self.assertEqual(compare(self.reports)["result"], "PASS")
        self.reports[2] = receipt(2, 80)
        result = compare(self.reports)
        self.assertEqual(result["result"], "FAIL")
        self.assertEqual([pair["passed"] for pair in result["pairs"]], [True, False])

    def test_rejects_same_code_and_overlapping_arms(self):
        for mutate, reason in (
            (lambda arms: [row.update(source_sha256="same") for row in arms], "distinct stable"),
            (lambda arms: arms[1]["before"].update(mono_ns=arms[0]["after"]["mono_ns"]), "overlap"),
        ):
            with self.subTest(reason=reason):
                arms = deepcopy(self.reports)
                mutate(arms)
                with self.assertRaisesRegex(ValueError, reason):
                    compare(arms)

    def test_rejects_missing_sample_or_changed_fixture(self):
        report = self.reports[0]
        with self.assertRaisesRegex(ValueError, "samples"):
            observations(dict(report, operations=report["operations"][1:]))
        report["operations"][-1]["frame_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "fixture pixels"):
            observations(report)

    def test_unwitnessed_owned_identity_cannot_qualify(self):
        with self.assertRaisesRegex(ValueError, "witness"):
            observations(dict(self.reports[0], owned_identity_verified=False))

    def test_rejects_missing_host_sensor_confounded_pair_and_drift(self):
        arms = deepcopy(self.reports)
        arms[1]["operations"][1]["host"]["cpu_perf_limited"].clear()
        with self.assertRaisesRegex(ValueError, "throttle"):
            compare(arms)
        arms = deepcopy(self.reports)
        arms[1]["after"]["temperature_millicelsius"]["thermal_zone0"] = 56000
        self.assertEqual(compare(arms)["result"], "INVALID")
        self.reports[1]["inputs"]["driver"] = "changed"
        with self.assertRaisesRegex(ValueError, "inputs"):
            compare(self.reports)

    def test_mutants_omit_privacy_read_or_digest_verification(self):
        for stage in ("privacy_metadata_read", "privacy_sentinel", "frame_sha256",
                      "png_validation_and_pixel_digest", "protected_frame_contract"):
            with self.subTest(stage=stage):
                report = deepcopy(self.reports[0])
                report["operations"][-1]["stage_calls"][stage] -= 1
                with self.assertRaisesRegex(ValueError, "stage missing"):
                    observations(report)

    def test_mutant_captures_or_publishes_login_fixture(self):
        for counter in ("acquisitions", "publications"):
            with self.subTest(counter=counter):
                report = deepcopy(self.reports[0])
                report["privacy_denial"][counter] = 1
                with self.assertRaisesRegex(ValueError, "privacy witness"):
                    observations(report)


if __name__ == "__main__":
    unittest.main()
