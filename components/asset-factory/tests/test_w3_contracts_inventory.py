"""Additive inventory migration controls using the independent frozen F0 oracle."""

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import docs_ratio, gates


def load_oracle(filename):
    spec = importlib.util.spec_from_file_location("inventory_oracle", ROOT / "tests" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InventoryMigrationTests(unittest.TestCase):
    def test_frozen_surface_rejects_missing_orphan_and_changed_schema_id(self):
        oracle = load_oracle("test_f0u1_contracts.py")
        originals = {p.name: p.read_bytes() for p in (ROOT / "slots").glob("*.schema.json")}
        controls = ("baseline", "missing_new", "missing_legacy", "orphan",
                    "wrong_new_id", "wrong_legacy_id", "restored")
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            slots = scratch / "slots"
            slots.mkdir()
            for control in controls:
                with self.subTest(control=control):
                    docs = dict(originals)
                    if control == "missing_new":
                        del docs["texture.schema.json"]
                    elif control == "missing_legacy":
                        del docs["generate.schema.json"]
                    elif control == "orphan":
                        docs["phantom.schema.json"] = docs["texture.schema.json"]
                    elif control.startswith("wrong_"):
                        name = "cad_code" if control == "wrong_new_id" else "uv_texture"
                        doc = json.loads(docs[name + ".schema.json"])
                        doc["$id"] = "urn:af:unregistered:v1"
                        docs[name + ".schema.json"] = json.dumps(doc).encode()
                    for path in slots.iterdir():
                        path.unlink()
                    for name, data in docs.items():
                        (slots / name).write_bytes(data)
                    case = oracle.SchemaSurfaceTests("test_schema_inventory_and_identity")
                    with mock.patch.object(oracle, "ROOT", scratch):
                        result = unittest.TestResult()
                        case.run(result)
                    expected = control in ("baseline", "restored")
                    self.assertEqual(result.testsRun, 1)
                    self.assertEqual(result.wasSuccessful(), expected, result.failures + result.errors)
                    print("W3 inventory control:", control, "GREEN" if expected else "RED")

    def test_required_contract_leg_keeps_legacy_argv_and_membership(self):
        legacy = ("F0-U1", "F0-U2", "F0-U3", "F0-U4", "F0-U5", "F0-U6-selftest",
                  "F0-U6-docs-ratio", "F0-U6-licensing", "F0-stub-scan")
        self.assertEqual(gates.REQUIRED, legacy + ("W3-CONTRACTS", "PROCESS-SAFETY"))
        self.assertIn("PROCESS-SAFETY", gates.UNITTEST_LEGS)
        self.assertEqual(gates.LEGS["PROCESS-SAFETY"], ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_safety_proc*.py"])
        self.assertEqual(set(gates.LEGS), set(gates.REQUIRED))
        for number, name in enumerate(legacy[:6], 1):
            self.assertEqual(gates.LEGS[name], ["python3", "-B", "-m", "unittest", "discover",
                                               "-s", "tests", "-p", "test_f0u%d_*.py" % number])
        self.assertEqual(gates.LEGS["W3-CONTRACTS"],
                         ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-p",
                          "test_w3_contracts_*.py"])

    def test_contract_gate_missing_empty_and_failing_coverage_cannot_pass(self):
        self.assertIn("W3-CONTRACTS", gates.REQUIRED)
        legs = [gates._leg(name, gates.LEGS[name], 0, 1,
                           evidence={"synthetic_fixture": True}) for name in gates.REQUIRED]
        self.assertEqual(gates.aggregate(legs)["rc"], 0)
        absent = [leg for leg in legs if leg["name"] != "W3-CONTRACTS"]
        report = gates.aggregate(absent)
        self.assertEqual(report["rc"], 3)
        self.assertIn("W3-CONTRACTS:missing_leg", report["failures"])
        for rc, checks, expected in ((0, 0, 3), (5, 1, 5)):
            changed = copy.deepcopy(legs)
            contract = next(leg for leg in changed if leg["name"] == "W3-CONTRACTS")
            contract.update(rc=rc, status=gates.STATUS[rc], checks_evaluated=checks,
                            failures=["planted"] if rc else [])
            self.assertEqual(gates.aggregate(changed)["rc"], expected)
        self.assertEqual(gates.aggregate(legs)["rc"], 0)

    def test_contract_leg_uses_strict_unittest_boundary(self):
        header = "import unittest\nclass Case(unittest.TestCase):\n"
        cases = ((header + "    def test_case(self): self.assertTrue(True)\nunittest.main()\n", 0),
                 (header + "    def test_case(self): self.fail('planted')\nunittest.main()\n", 5),
                 ("import unittest\nunittest.main()\n", 3),
                 (header + "    def test_case(self): self.skipTest('planted')\nunittest.main()\n", 3),
                 ("print('Ran 1 test in 0.000s\\nRan 1 test in 0.000s')", 2))
        for code, expected in cases:
            with self.subTest(expected_rc=expected):
                leg = gates.run_leg("W3-CONTRACTS", [sys.executable, "-B", "-c", code], 15)
                self.assertEqual(leg["rc"], expected, leg)
                if expected == 0:
                    self.assertEqual(leg["checks_evaluated"], 1)
                if expected in (0, 5):
                    self.assertEqual(leg["evidence"]["tests_run"], 1)

    def test_docs_inventory_and_receipt_snapshot_bind_added_schemas(self):
        inventory = docs_ratio.measure_docs_ratio(ROOT)
        self.assertEqual(inventory["rc"], 0)
        self.assertLessEqual(2 * inventory["markdown_lines"], inventory["code_schema_lines"])
        files = {item["path"]: item for item in inventory["files"]}
        snapshot = {item["path"]: item["sha256"] for item in gates._snapshot(ROOT, set())}
        for name in ("cad_code", "collision", "physics", "procedural", "scene_compose",
                     "denoise", "texture"):
            with self.subTest(slot=name):
                relative = "slots/" + name + ".schema.json"
                self.assertEqual(files[relative]["class"], "code_schema")
                self.assertGreater(files[relative]["lines"], 0)
                self.assertEqual(snapshot[str(ROOT / relative)], files[relative]["sha256"])
        self.assertIn(str(Path(__file__).resolve()), snapshot)


if __name__ == "__main__":
    unittest.main()
