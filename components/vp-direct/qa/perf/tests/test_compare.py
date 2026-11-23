"""Exercise the ABBA comparison against synthetic receipts."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compare as perf


def arm_receipt(arm, offset):
    def host(tick, busy):
        return dict(mono_ns=offset + tick, loadavg=[1, 1, 1], mem_available_kib=21 << 20,
                    cpu_perf_limited={"16": 0}, processor_cooling_state={"cpu": 0},
                    temperature_millicelsius={"cpu": 50_000},
                    cpu_ticks={f"cpu{cpu}": [busy, 0, 0, 100, 0, 0, 0, 0] for cpu in range(16)})

    blocks = []
    for block, profile in enumerate(perf.PROFILES):
        rows = []
        for index, stratum in enumerate(("cold", "warmup", "warm", "warm")):
            start = offset + 10 * (1 + len(rows) + 4 * block)
            rows.append(dict(stratum=stratum, state_before_source="ACTIVE", frame_sha256="ab"[index % 2],
                             changed=True if index else None, submit_ns=start, admit_ns=start + 1,
                             first_byte_ns=start + 2, complete_ns=start + 5, end_to_end_ns=5,
                             host=host(start - offset + 6, 75)))
        blocks.append(dict(profile=profile, frame_workload="changing", fixture_sha256="c",
                           alternate_fixture_sha256="d", frame_sha256="a", alternate_frame_sha256="b",
                           refusals=[dict(name="pool_saturation", reason="session capacity exhausted")],
                           runs=rows))
    return dict(arm=arm, failures=[], clock="CLOCK_MONOTONIC", cpu_affinity=[16, 17, 18, 19],
                limits_bytes={"memory.high": 6 << 30, "memory.max": 8 << 30}, pool_sha256=arm,
                base_commit="base", binding_sha256="binding", stream_sha256="stream", seed=521,
                warmups=1, benchmark_sha256="benchmark", warm_samples_per_state_per_block=2,
                blocks=blocks, before=host(1, 50), after=host(190, 100))


class ChangingFrameComparisonTests(unittest.TestCase):
    def setUp(self):
        self.arms = [arm_receipt(arm, 200 * index) for index, arm in enumerate(perf.ARMS)]

    def test_complete_abba_changing_series_excludes_cold_and_warmup(self):
        row = self.arms[1]["blocks"][0]["runs"][1]
        row["complete_ns"] += 2
        row["end_to_end_ns"] += 2
        result = perf.compare(self.arms)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual([pair["samples_per_arm"] for pair in result["pairs"]], [4] * 4)

    def test_rejected_receipt_is_outside_source_tree(self):
        self.assertFalse((Path(perf.__file__).parent / "runner-rejected.json").exists())

    def test_regression_in_one_pair_fails_even_when_other_pairs_pass(self):
        row = self.arms[1]["blocks"][0]["runs"][2]
        row["complete_ns"] += 1
        row["end_to_end_ns"] += 1
        self.assertEqual([pair["result"] for pair in perf.compare(self.arms)["pairs"]],
                         ["FAIL", "PASS", "PASS", "PASS"])
        self.assertEqual(perf.compare(self.arms)["result"], "FAIL")

    def test_mutant_accepting_repeated_pixels_would_pass(self):
        self.arms[1]["blocks"][0]["runs"][3]["frame_sha256"] = "a"
        require = perf.require

        def omit_frame_oracle(condition, reason):
            if reason != "frame did not alternate":
                require(condition, reason)

        with self.assertRaisesRegex(ValueError, "frame did not alternate"):
            perf.compare(self.arms)
        with patch.object(perf, "require", omit_frame_oracle):
            self.assertEqual(perf.compare(self.arms)["result"], "PASS")

    def test_competing_occupancy_invalidates_matched_latency(self):
        for ticks in self.arms[1]["after"]["cpu_ticks"].values():
            ticks[3] += 50
        result = perf.compare(self.arms)
        self.assertEqual(result["result"], "INVALID")
        self.assertEqual([entry["metric"] for entry in result["confounds"]], ["occupancy"])

    def test_missing_stratum_or_reordered_arms_cannot_pass(self):
        with self.assertRaisesRegex(ValueError, "complete ABBA order required"):
            perf.compare(self.arms[::-1][1:] + self.arms[:1])
        self.arms[2]["blocks"].pop()
        with self.assertRaisesRegex(ValueError, "missing or out-of-order changing-frame profiles"):
            perf.compare(self.arms)


class UnchangedFrameComparisonTests(unittest.TestCase):
    def setUp(self):
        self.arms = [arm_receipt(arm, 600 * index) for index, arm in enumerate(perf.ARMS)]
        for arm in self.arms:
            for index, block in enumerate(arm["blocks"]):
                block["frame_workload"] = "unchanged"
                asleep = dict(name="retired_asleep_token", reason="stale stream token")
                off = dict(name="retired_off_token", reason="stale stream token")
                block["refusals"] = [perf.SATURATION, *([asleep] * 4), perf.SATURATION,
                                     *([off] * 3), perf.SATURATION]
                original = block["runs"]
                rows = []
                for state in ("ASLEEP", "OFF", "ACTIVE"):
                    for row_index, original_row in enumerate(original):
                        row = deepcopy(original_row)
                        start = arm["before"]["mono_ns"] + 10 * (1 + index * 12 + len(rows))
                        elapsed = 5 if arm["arm"] == "baseline" or state != "ACTIVE" else 4
                        row.update(state_before_source=state, frame_sha256="a",
                                   changed=False if state == "ACTIVE" and row_index else None,
                                   submit_ns=start, admit_ns=start + 1, first_byte_ns=start + 2,
                                   complete_ns=start + elapsed, end_to_end_ns=elapsed)
                        row["host"]["mono_ns"] = start + 6
                        rows.append(row)
                block["runs"] = rows
            arm["after"]["mono_ns"] = arm["before"]["mono_ns"] + 550

    def test_persistent_warm_1080p_median_improvement(self):
        result = perf.compare(self.arms, workload="unchanged")
        self.assertEqual(result["result"], "PASS")
        self.assertEqual([pair["samples_per_arm"] for pair in result["pairs"]], [4, 4])
        for block in self.arms[2]["blocks"][1:3]:
            for row in block["runs"][-2:]:
                row["end_to_end_ns"] += 1
                row["complete_ns"] += 1
        result = perf.compare(self.arms, workload="unchanged")
        self.assertEqual([pair["result"] for pair in result["pairs"]], ["PASS", "FAIL"])
        self.assertEqual(result["result"], "FAIL")

    def test_refusals_and_wake_markers_cannot_be_omitted(self):
        block = self.arms[1]["blocks"][0]
        refusal = block["refusals"].pop(1)
        with self.assertRaisesRegex(ValueError, "unchanged-frame refusal sequence differs"):
            perf.compare(self.arms, workload="unchanged")
        block["refusals"].insert(1, refusal)
        block["runs"][4].pop("first_byte_ns")
        with self.assertRaises(KeyError):
            perf.compare(self.arms, workload="unchanged")


if __name__ == "__main__":
    unittest.main()
