"""S1-U1 fail-closed outcome, publication and process lifecycle controls."""

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from asset_factory import core
from providers.blender_sculpt.probe import run as probe


def stroke(strength=500000):
    return {"rc": 0, "status": "OK", "reason_codes": [], "runtime_version": "4.0.2",
            "operator_poll": True, "operator_return": ["FINISHED"],
            "execution_context": {"background": False, "mode": "SCULPT"},
            "strength_ppm": strength, "before_sha256": "a" * 64,
            "after_sha256": ("a" if strength == 0 else "b") * 64,
            "before_vertex_sha256": "a" * 64,
            "after_vertex_sha256": ("a" if strength == 0 else "b") * 64,
            "changed_vertices": 0 if strength == 0 else 257,
            "mesh_unchanged": strength == 0}


class OutcomeTests(unittest.TestCase):
    def verify(self, row, strength=500000):
        return probe.verify_stroke(row, strength_ppm=strength, background=False)

    def test_nonzero_observed_effect_passes(self):
        self.assertEqual(self.verify(stroke())["rc"], 0)

    def test_zero_strength_preserves_digest(self):
        self.assertEqual(self.verify(stroke(0), 0)["rc"], 0)

    def test_finished_with_no_effect_is_fail(self):
        row = stroke()
        row.update(after_sha256=row["before_sha256"], mesh_unchanged=True)
        self.assertEqual(self.verify(row)["rc"], 5)

    def test_zero_strength_mutation_is_fail(self):
        row = stroke(0)
        row.update(after_sha256="b" * 64, mesh_unchanged=False)
        self.assertEqual(self.verify(row, 0)["rc"], 5)

    def test_false_poll_never_passes(self):
        row = stroke()
        row["operator_poll"] = False
        self.assertEqual(self.verify(row)["rc"], 3)

    def test_wrong_runtime_is_stale(self):
        row = stroke()
        row["runtime_version"] = "unqualified"
        self.assertEqual(self.verify(row)["rc"], 4)

    def test_missing_and_forged_digest_never_pass(self):
        for value in (None, "", "z" * 64):
            row = stroke()
            row["after_sha256"] = value
            self.assertEqual(self.verify(row)["rc"], 3)

    def test_inconsistent_unchanged_claim_is_error(self):
        row = stroke()
        row["mesh_unchanged"] = True
        self.assertEqual(self.verify(row)["rc"], 2)

    def test_wrong_background_cannot_substitute(self):
        row = stroke()
        row["execution_context"]["background"] = True
        self.assertEqual(self.verify(row)["rc"], 3)

    def test_missing_observation_and_bool_rc_are_not_success(self):
        self.assertEqual(self.verify(None)["rc"], 3)
        row = stroke()
        row["rc"] = False
        self.assertEqual(self.verify(row)["rc"], 2)

    def test_forbidden_one_is_error(self):
        row = stroke()
        row["rc"] = 1
        self.assertEqual(self.verify(row)["rc"], 2)

    def test_wrong_strength_cannot_substitute(self):
        self.assertEqual(self.verify(stroke(0))["rc"], 3)

    def test_pair_requires_same_fixture(self):
        rows = [stroke(0), stroke()]
        self.assertEqual(probe.qualify_pair(rows, background=False)["rc"], 0)
        rows[1]["before_sha256"] = "c" * 64
        self.assertEqual(probe.qualify_pair(rows, background=False)["rc"], 3)
        self.assertEqual(probe.qualify_pair([], background=False)["rc"], 3)

    def test_topology_only_change_is_not_a_sculpt_effect(self):
        row = stroke()
        row.update(after_vertex_sha256=row["before_vertex_sha256"], changed_vertices=0)
        self.assertEqual(self.verify(row)["rc"], 5)

    def test_bool_strength_is_not_zero(self):
        row = stroke(0)
        row["strength_ppm"] = False
        self.assertEqual(self.verify(row, 0)["rc"], 3)

    def test_partial_record_cannot_pass_by_invalid_verifier_arguments(self):
        self.assertEqual(probe.verify_stroke(stroke(0), strength_ppm=False, background=False)["rc"], 2)

    def test_duplicate_float_and_nan_records_are_refused(self):
        for raw in ('{"rc":0,"rc":0}', '{"value":0.0}', '{"value":NaN}'):
            with self.assertRaises(ValueError):
                probe.parse_record(raw)

    def test_mutant_accepting_no_effect_goes_red_then_nominal_green(self):
        def oracle():
            class EffectOracle(unittest.TestCase):
                def runTest(inner):
                    row = stroke()
                    row.update(after_sha256=row["before_sha256"], mesh_unchanged=True)
                    inner.assertEqual(probe.verify_stroke(row, strength_ppm=500000, background=False)["rc"], 5)
            return unittest.TextTestRunner(stream=io.StringIO()).run(EffectOracle())
        with mock.patch.object(probe, "verify_stroke", return_value=probe.result(0)):
            red = oracle()
        self.assertEqual(len(red.failures), 1)
        self.assertFalse(red.wasSuccessful())
        self.assertTrue(oracle().wasSuccessful())

    def test_mutant_ignoring_zero_mutation_goes_red_then_nominal_green(self):
        row = stroke(0)
        row.update(after_sha256="b" * 64, mesh_unchanged=False)
        with mock.patch.object(probe, "verify_stroke", return_value=probe.result(0)):
            with self.assertRaises(AssertionError):
                self.assertEqual(self.verify(row, 0)["rc"], 5)
        self.assertEqual(self.verify(row, 0)["rc"], 5)


class LifecycleTests(unittest.TestCase):
    def test_timeout_stops_owned_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = probe.process_case([sys.executable, "-c", "import time; time.sleep(20)"],
                                     directory=Path(tmp), timeout_s=1)
            self.assertEqual(row["rc"], 6)
            self.assertLess(row["process_rc"], 0)
            self.assertLess(row["wall_ms"], 8000)

    def test_missing_record_is_cannot_see(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = probe.process_case([sys.executable, "-c", "pass"], directory=Path(tmp), timeout_s=2)
            self.assertEqual(row["rc"], 3)

    def test_invalid_input_preserves_caller_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "receipt.json"
            target.write_bytes(b"caller bytes\n")
            with mock.patch("sys.stdout", io.StringIO()):
                rc = probe.main(["--blender", str(root / "missing"), "--output-dir", str(root),
                                 "--bvqa-root", str(root), "--receipt", str(target)])
            self.assertEqual(rc, 2)
            self.assertEqual(target.read_bytes(), b"caller bytes\n")

    def test_incomplete_run_preserves_caller_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "receipt.json"
            target.write_bytes(b"caller bytes\n")
            with mock.patch.object(probe, "run", return_value=probe.result(3)), mock.patch("sys.stdout", io.StringIO()):
                rc = probe.main(["--blender", sys.executable, "--output-dir", tmp,
                                 "--bvqa-root", tmp, "--receipt", str(target)])
            self.assertEqual(rc, 3)
            self.assertEqual(target.read_bytes(), b"caller bytes\n")

    def test_successful_publication_is_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "receipt.json"
            row = probe.result(0, unit="S1-U1")
            with mock.patch.object(probe, "run", return_value=row), mock.patch("sys.stdout", io.StringIO()):
                rc = probe.main(["--blender", sys.executable, "--output-dir", tmp,
                                 "--bvqa-root", tmp, "--receipt", str(target)])
            self.assertEqual(rc, 0)
            self.assertEqual(target.read_text(), core.canonical_json(row))

    def test_invalid_timeout_is_error(self):
        with self.assertRaises(core.ContractError):
            probe.run(blender=sys.executable, output_dir=Path.cwd(), bvqa_root=Path.cwd(), timeout_s=0)


if __name__ == "__main__":
    unittest.main()
