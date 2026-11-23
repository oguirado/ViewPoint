"""Adversarial aggregation and live child-output regression controls."""
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

from asset_factory import proc as af_proc
from gates.sculpt_metric import api
from tools.mesh import contracts as mc
from test_ws_s1_u3_metric import valid_mesh, valid_vision, region_policy


def permissive_policy():
    return dict(region_policy(), max_changed_pixels_ppm=1000000,
                max_mean_abs_delta_ppm=1000000)


def changed_vision():
    vision = valid_vision()
    vision["views"][1].update(candidate_frame_sha256="b" * 64,
                             changed_pixels=1, changed_pixels_ppm=245,
                             mean_abs_delta_ppm=1)
    return vision


def covered_vision():
    vision = changed_vision()
    vision["views"][1].update(
        regions=[{"id": "region-one", "spans": [["0000", "0000"]]}],
        region_scores=[{"id": "region-one", "spans": [["0000", "0000"]],
                        "pixel_count": 256, "changed_pixels": 1,
                        "changed_pixels_ppm": 3907, "mean_abs_delta_ppm": 6,
                        "score_ppm": 996093}])
    return vision


class AggregationRepairTests(unittest.TestCase):
    def combine(self, measured=None, vision=None):
        return api.combine(valid_mesh() if measured is None else measured,
                           valid_vision() if vision is None else vision,
                           region_policy=permissive_policy())

    def test_changed_pixels_need_regions_under_permissive_budgets(self):
        rc, reasons, _ = self.combine(vision=changed_vision())
        self.assertEqual(rc, 3)
        self.assertIn("back:region_coverage_incomplete", reasons)

    def test_identity_frame_cannot_claim_nonzero_residual(self):
        vision = valid_vision()
        vision["views"][0]["mean_abs_delta_ppm"] = 1
        with self.assertRaises(api.MetricError) as caught:
            self.combine(vision=vision)
        self.assertEqual((caught.exception.rc, caught.exception.reason),
                         (2, "residual_count_invalid"))

    def test_changed_frame_cannot_claim_zero_residual(self):
        vision = covered_vision()
        vision["views"][1]["mean_abs_delta_ppm"] = 0
        with self.assertRaises(api.MetricError) as caught:
            self.combine(vision=vision)
        self.assertEqual(caught.exception.rc, 2)

    def test_measured_region_and_identity_positive_controls(self):
        self.assertEqual(self.combine()[0], 0)
        self.assertEqual(self.combine(vision=covered_vision())[0], 0)

    def test_region_counts_cannot_disagree_with_whole_frame(self):
        vision = covered_vision()
        scored = vision["views"][1]["region_scores"][0]
        scored.update(changed_pixels=2, changed_pixels_ppm=7813, mean_abs_delta_ppm=11,
                      score_ppm=992187)
        with self.assertRaises(api.MetricError) as caught:
            self.combine(vision=vision)
        self.assertEqual((caught.exception.rc, caught.exception.reason),
                         (2, "region_coverage_invalid"))

    def test_region_pixel_count_and_score_are_recomputed(self):
        for field, value in (("pixel_count", True), ("changed_pixels", True),
                             ("changed_pixels_ppm", 0), ("mean_abs_delta_ppm", 0),
                             ("score_ppm", 1000000)):
            with self.subTest(field=field):
                vision = covered_vision()
                vision["views"][1]["region_scores"][0][field] = value
                with self.assertRaises(api.MetricError) as caught:
                    self.combine(vision=vision)
                self.assertEqual(caught.exception.rc, 2)

    def test_duplicate_region_cannot_double_count_coverage(self):
        vision = covered_vision()
        row = vision["views"][1]
        row["regions"] *= 2
        row["region_scores"] *= 2
        with self.assertRaises(api.MetricError) as caught:
            self.combine(vision=vision)
        self.assertEqual(caught.exception.rc, 2)

    def test_success_requires_nonempty_passing_topology_inventory(self):
        for inventories in ([], [{"components": 0, "failed": []}],
                            [{"components": 1, "failed": ["boundary"]}]):
            with self.subTest(inventories=inventories):
                measured = valid_mesh()
                measured["metrics"]["component_inventories"] = inventories
                with self.assertRaises(mc.MeshError) as caught:
                    self.combine(measured=measured)
                self.assertEqual(caught.exception.rc, 2)


class ChildBudgetRepairTests(unittest.TestCase):
    def run_overflow(self, script):
        import tempfile
        children = []
        spawn = af_proc.popen
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        marker = Path(folder.name)/'payload.pid'
        script = ('import os;from pathlib import Path;Path(' + repr(str(marker)) +
                  ').write_text(str(os.getpid()));' + script)

        def owned_spawn(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            return child

        with mock.patch.object(api.af_proc, "popen", side_effect=owned_spawn):
            with self.assertRaises(api.MetricError) as caught:
                api._child([sys.executable, "-I", "-c", script],
                           deadline=time.monotonic() + 5)
        self.assertEqual((caught.exception.rc, caught.exception.reason),
                         (6, "child_output_budget"))
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(af_proc.poll_exit(children[0]))
        pid = int(marker.read_text())
        self.assertGreater(pid, 1)
        self.assertFalse(Path(f"/proc/{pid}").exists())

    def test_stderr_overflow_rejected(self):
        self.run_overflow("import sys; sys.stderr.buffer.write(b'x'*(4*2**20+1))")

    def test_stdout_overflow_stops_writer_live(self):
        self.run_overflow("import sys,time; sys.stdout.buffer.write(b'x'*(4*2**20+1)); "
                          "sys.stdout.flush(); time.sleep(30)")

    def test_stderr_overflow_stops_writer_live(self):
        self.run_overflow("import sys,time; sys.stderr.buffer.write(b'x'*(4*2**20+1)); "
                          "sys.stderr.flush(); time.sleep(30)")

    def test_combined_stream_budget_is_enforced(self):
        self.run_overflow("import sys; sys.stderr.buffer.write(b'x'*(2*2**20)); "
                          "sys.stderr.flush(); sys.stdout.buffer.write(b'x'*(2*2**20+1))")

    def test_input_and_both_streams_below_limit(self):
        payload = b'p' * 131072
        rc, out = api._child(
            [sys.executable, "-I", "-c", "import sys; data=sys.stdin.buffer.read(); "
             "sys.stderr.buffer.write(b'diagnostic'); sys.stdout.buffer.write(data)"],
            payload=payload, deadline=time.monotonic() + 5)
        self.assertEqual((rc, out), (0, payload))


if __name__ == "__main__":
    unittest.main()
