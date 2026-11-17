"""Receipt-chain/cache controls. Synthetic backends are explicitly test-only."""
import copy
import io
import contextlib
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asset_factory import core
from tools.pipeline import engine, contracts, run_asset


class SyntheticStages:
    scope = "synthetic-chain-test"

    def __init__(self, root):
        self.source, self.reference = root / "source", root / "reference"
        self.source.write_bytes(b"synthetic GLB stand-in")
        self.reference.write_bytes(b"synthetic reference stand-in")
        self.calls, self.failure, self.hook = [], {}, None
        self.parameters = {name: {} for name in engine.STAGES}

    def identity(self, name, subject):
        refs = []
        if name == "provider":
            refs.append(engine.file_ref(self.source))
        if subject:
            refs.append(engine.file_ref(subject))
        if name == "appearance":
            refs.append(engine.file_ref(self.reference))
        return refs, copy.deepcopy(self.parameters[name]), core.sha256_json({"test_backend": 1})

    def execute(self, name, directory, subject):
        self.calls.append(name)
        if self.hook:
            self.hook(name, directory)
        failure = self.failure.get(name)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return {"rc": failure, "reason_codes": ["synthetic_refusal"]}
        raw = (subject.read_bytes() if subject else self.source.read_bytes()) + name.encode()
        (directory / "asset.glb").write_bytes(raw)
        core.atomic_write_json(directory / "native.json", {"schema": "synthetic-only", "rc": 0})
        return {"rc": 0, "subject": "asset.glb", "outputs": ["asset.glb"],
                "evidence": ["native.json"], "checks_evaluated": 1}

    def verify_sources(self):
        pass


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("AF_E2E_OUTPUT"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = SyntheticStages(self.root)
        self.output = self.root / "output"
        self.pipeline = engine.Pipeline(self.output, self.backend)

    def run_pipeline(self):
        result = self.pipeline.run()
        core.validate(result, contracts.RUN)
        self.assertNotEqual(result["rc"], 1)
        return result

    def first(self):
        result = self.run_pipeline()
        self.assertEqual(result["rc"], 0, result)
        return result

    def test_order_and_receipt_hash_links(self):
        result = self.first()
        self.assertEqual(self.backend.calls, list(engine.STAGES))
        previous = None
        for row in result["stages"]:
            engine.authenticate(row["receipt"])
            receipt = engine.load(row["receipt"]["path"])
            self.assertEqual(receipt["previous"], previous)
            self.assertEqual(receipt["verdict"], "PASS")
            self.assertEqual(Path(row["receipt"]["path"]).read_text(), core.canonical_json(receipt))
            previous = row["receipt"]
        self.assertEqual(result["scope"], "synthetic-chain-test")

    def test_identical_run_reuses_every_receipt_without_calls(self):
        first = self.first()
        self.backend.calls.clear()
        second = self.first()
        self.assertEqual(self.backend.calls, [])
        self.assertTrue(all(row["cache_hit"] for row in second["stages"]))
        self.assertEqual([r["receipt"] for r in first["stages"]], [r["receipt"] for r in second["stages"]])

    def test_reference_change_invalidates_appearance_downstream_only(self):
        self.first()
        self.backend.calls.clear()
        self.backend.reference.write_bytes(b"changed reference")
        second = self.first()
        self.assertEqual(self.backend.calls, ["appearance", "vqa3d"])
        self.assertTrue(all(row["cache_hit"] for row in second["stages"][:6]))

    def test_source_change_invalidates_provider_downstream_only(self):
        self.first()
        self.backend.calls.clear()
        self.backend.source.write_bytes(b"changed GLB")
        self.first()
        self.assertEqual(self.backend.calls, list(engine.STAGES[1:]))

    def test_parameter_change_invalidates_its_stage_downstream(self):
        self.first()
        self.backend.calls.clear()
        self.backend.parameters["topology"]["target_faces"] = 12
        self.first()
        self.assertEqual(self.backend.calls, list(engine.STAGES[3:]))

    def test_deleted_upstream_receipt_refuses_cache_and_downstream(self):
        first = self.first()
        Path(first["stages"][2]["receipt"]["path"]).unlink()
        self.backend.calls.clear()
        result = self.run_pipeline()
        self.assertEqual(result["rc"], 3)
        self.assertEqual(self.backend.calls, [])
        self.assertIsNone(result["artifact"])

    def test_tampered_glb_bytes_refuse_without_recompute(self):
        first = self.first()
        record = engine.load(first["stages"][4]["receipt"]["path"])
        Path(record["subject"]).write_bytes(b"tampered GLB")
        self.backend.calls.clear()
        result = self.run_pipeline()
        self.assertEqual(result["stages"][4]["rc"], 4)
        self.assertEqual(result["verdict"], "CANNOT_SEE")
        self.assertEqual(self.backend.calls, [])

    def test_cannot_see_stage_stops_all_downstream(self):
        self.backend.failure["topology"] = 3
        result = self.run_pipeline()
        self.assertEqual(result["verdict"], "CANNOT_SEE")
        self.assertEqual(self.backend.calls, list(engine.STAGES[:4]))
        self.assertTrue(all(row["rc"] == 3 for row in result["stages"][3:]))

    def test_failed_stage_is_not_cached_and_recovers_with_evidence(self):
        self.backend.failure["provider"] = 3
        self.assertEqual(self.run_pipeline()["rc"], 3)
        self.backend.failure.clear()
        self.backend.calls.clear()
        self.first()
        self.assertEqual(self.backend.calls, list(engine.STAGES[1:]))

    def test_stale_cache_entry_goes_red(self):
        first = self.first()
        target = Path(first["stages"][3]["receipt"]["path"]).parent / "entry.json"
        marker = engine.load(target)
        marker["key"] = "0" * 64
        core.atomic_write_json(target, marker)
        self.backend.calls.clear()
        result = self.run_pipeline()
        self.assertEqual(result["stages"][3]["rc"], 4)
        self.assertEqual(self.backend.calls, [])

    def test_provider_timeout_is_budget_exceeded_not_pass(self):
        self.backend.failure["provider"] = TimeoutError("injected provider timeout")
        result = self.run_pipeline()
        self.assertEqual(result["stages"][1]["rc"], 6)
        self.assertEqual(result["verdict"], "CANNOT_SEE")
        self.assertEqual(self.backend.calls, ["input", "provider"])

    def test_native_fail_error_and_invalid_rcs(self):
        for value, want in [(5, 5), (2, 2), (1, 2), (True, 2)]:
            with self.subTest(value=value):
                self.backend.failure["provider"] = value
                self.assertEqual(self.run_pipeline()["rc"], want)

    def test_missing_or_malformed_native_evidence_cannot_pass(self):
        original = self.backend.execute
        def empty(name, directory, subject):
            result = original(name, directory, subject)
            result["checks_evaluated"] = 0
            return result
        with mock.patch.object(self.backend, "execute", empty):
            self.assertEqual(self.run_pipeline()["rc"], 2)

    def test_mid_stage_deleted_upstream_receipt_stops_publication(self):
        def delete(name, directory):
            if name == "mesh":
                next((self.output / "cache" / "provider").glob("*/receipt.json")).unlink()
        self.backend.hook = delete
        result = self.run_pipeline()
        self.assertNotEqual(result["rc"], 0)
        self.assertFalse(list((self.output / "cache" / "mesh").glob("*/entry.json")))
        self.assertEqual(self.backend.calls, ["input", "provider", "mesh"])

    def test_mid_stage_tampered_source_stops_publication(self):
        def tamper(name, directory):
            if name == "provider":
                self.backend.source.write_bytes(b"mutation during execution")
        self.backend.hook = tamper
        result = self.run_pipeline()
        self.assertEqual(result["stages"][1]["rc"], 4)
        self.assertEqual(self.backend.calls, ["input", "provider"])

    def test_duplicate_json_and_float_receipts_are_refused(self):
        first = self.first()
        path = Path(first["stages"][2]["receipt"]["path"])
        for bad in (b'{"rc":0,"rc":0}', b'{"rc":0.0}'):
            path.write_bytes(bad)
            result = self.run_pipeline()
            self.assertNotEqual(result["rc"], 0)

    def test_malformed_resealed_receipt_stops_before_execution(self):
        first = self.first()
        receipt = Path(first["stages"][2]["receipt"]["path"])
        receipt.write_bytes(b'{"schema":"af.pipeline-stage/v1","rc":0}\n')
        marker_path = receipt.parent / "entry.json"
        marker = engine.load(marker_path)
        marker["receipt"] = engine.file_ref(receipt)
        core.atomic_write_json(marker_path, marker)
        self.backend.calls.clear()
        self.assertEqual(self.run_pipeline()["stages"][2]["rc"], 2)
        self.assertEqual(self.backend.calls, [])

    def test_symlinked_cache_refuses_and_preserves_caller_bytes(self):
        self.output.mkdir()
        caller = self.root / "caller"
        caller.mkdir()
        (caller / "sentinel").write_bytes(b"caller bytes")
        (self.output / "cache").symlink_to(caller, target_is_directory=True)
        self.assertEqual(self.run_pipeline()["rc"], 2)
        self.assertEqual((caller / "sentinel").read_bytes(), b"caller bytes")
        self.assertEqual([p.name for p in caller.iterdir()], ["sentinel"])

    def test_mutant_disabling_byte_authentication_breaks_control(self):
        first = self.first()
        record = engine.load(first["stages"][2]["receipt"]["path"])
        Path(record["subject"]).write_bytes(b"broken")
        def disabled(ref, *, inside=None):
            pass
        with mock.patch.object(engine, "authenticate", disabled):
            # With the meter disabled stale bytes get reused. This assertion
            # proves the real stale-byte control can distinguish the mutation.
            result = self.run_pipeline()
        self.assertTrue(result["stages"][2]["cache_hit"])
        with self.assertRaises(AssertionError):
            self.assertEqual(result["stages"][2]["rc"], 4, "disabled meter falsely reused tampered bytes")

    def test_cli_usage_never_rc1(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_asset.main([]), 2)


if __name__ == "__main__":
    unittest.main()
