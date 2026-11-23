"""Real Blender/adapter qualification; unavailable runtimes fail this gate."""

from pathlib import Path
import tempfile
import unittest

from asset_factory import core
from providers.blender_sculpt.probe import run as probe


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory()
        component = Path(__file__).resolve().parents[1]
        cls.report = probe.run(blender=Path("/usr/bin/blender"), output_dir=Path(cls.workspace.name),
                               bvqa_root=component.parent / "blender-visual-qa", timeout_s=20)

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def test_rna_dump_is_bound_and_repeatable(self):
        report = self.report
        self.assertTrue(report["rna_repeat_verified"])
        for row in report["headless_observations"][:2]:
            self.assertEqual(row["rna_sha256"], core.sha256_json(row["rna"]))
        identifiers = {p["identifier"] for p in report["headless_observations"][0]["rna"]["properties"]}
        self.assertEqual(identifiers, {"rna_type", "stroke", "mode", "ignore_background_click"})

    def test_unknown_operator_is_exact_rc_two(self):
        self.assertTrue(self.report["unknown_operator_control"])
        row = self.report["headless_observations"][2]
        self.assertEqual((row["rc"], row["reason_codes"]), (2, ["unknown_operator"]))

    def test_live_null_and_effect_on_same_mesh(self):
        rows = self.report["live_observations"]
        self.assertEqual(self.report["path_b_live"]["rc"], 0, self.report["path_b_live"])
        self.assertEqual(len(rows), 2)
        null, effect = rows
        self.assertEqual(null["before_sha256"], null["after_sha256"])
        self.assertEqual(null["after_sha256"], effect["before_sha256"])
        self.assertNotEqual(effect["before_sha256"], effect["after_sha256"])
        self.assertEqual(null["changed_vertices"], 0)
        self.assertGreater(effect["changed_vertices"], 0)

    def test_headless_refusals_do_not_claim_execution(self):
        for row in self.report["headless_observations"][3:6]:
            self.assertEqual(row["rc"], 3)
            self.assertFalse(row["operator_poll"])
            self.assertEqual(row["before_sha256"], row["after_sha256"])

    def test_capability_gate_and_live_process_cleanup(self):
        self.assertEqual(self.report["rc"], 0, self.report["reason_codes"])
        directory = Path(self.report["evidence_dir"])
        lifecycle = core.load_json(directory / "live/lifecycle.json", max_bytes=100000)
        self.assertTrue(lifecycle["process_reaped"])
        self.assertEqual(self.report["path_b_live"]["transport"]["background"], False)
        self.assertTrue(self.report["path_b_live"]["actions"])


if __name__ == "__main__":
    unittest.main()
