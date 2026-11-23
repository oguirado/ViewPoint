"""Pipeline plan identity and accepted WIRE-D fixture integration."""
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from asset_factory import core
from test_e2e_pipeline import SyntheticStages
import test_n18_wire_d as wire_d
from tools.pipeline import engine


class PlanTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=os.environ.get("WIRE_INT1_EVIDENCE"))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "pipeline"
        self.native = SyntheticStages(self.root)

    def legacy_receipt_oracle(self):
        with patch.object(engine, "time") as clock:
            clock.monotonic_ns.return_value = 0
            run = engine.Pipeline(self.output, self.native).run()
        previous = None
        for name, row in zip(engine.STAGES, run["stages"], strict=True):
            record = engine.load(row["receipt"]["path"])
            subject = Path(engine.load(previous["path"])["subject"]) if previous else None
            inputs, params, pin = self.native.identity(name, subject)
            identity = {"stage": name, "inputs": inputs, "parameters": params,
                        "previous": previous, "toolchain_sha256": pin}
            directory = self.output / "cache" / name / core.sha256_json(identity)
            expected = identity | {
                "schema": "af.pipeline-stage/v1", "key": directory.name,
                "rc": 0, "status": "OK", "verdict": "PASS",
                "outputs": [engine.file_ref(directory / "asset.glb")],
                "evidence": [engine.file_ref(directory / "native.json")],
                "subject": str(directory / "asset.glb"), "checks_evaluated": 1,
                "wall_ms": 0, "peak_rss_bytes": None,
                "absence_reasons": {"peak_rss_bytes": "not_independently_measured"},
                "reason_codes": [],
            }
            self.assertEqual(record, expected)
            self.assertEqual(Path(row["receipt"]["path"]).read_bytes(),
                             core.canonical_json(expected).encode())
            previous = row["receipt"]
        self.assertEqual(run["run_key"], core.sha256_json([row["receipt"] for row in run["stages"]]))
        return run

    def test_default_matches_legacy_receipt_bytes(self):
        self.legacy_receipt_oracle()

    def test_explicit_prop_matches_default(self):
        default = self.legacy_receipt_oracle()
        shutil.rmtree(self.output)
        with patch.object(engine, "time") as clock:
            clock.monotonic_ns.return_value = 0
            explicit = engine.Pipeline(self.output, self.native, plan="prop").run()
        self.assertEqual(core.canonical_json(default), core.canonical_json(explicit))

    def test_unknown_plan_refuses_before_writing(self):
        with self.assertRaises(engine.Refusal) as refused:
            engine.Pipeline(self.output, self.native, plan="unknown")
        self.assertEqual((refused.exception.rc, refused.exception.reason), (2, "stage_plan_unknown"))
        self.assertFalse(self.output.exists())

    def test_execution_pass_does_not_override_production_verdict(self):
        self.native.parameters["export"] = {
            "production_verdict": "CANNOT_SEE", "calibration_state": "TARGET_UNCALIBRATED",
        }
        pipeline = engine.Pipeline(self.output, self.native)
        for cached in (False, True):
            with self.subTest(cached=cached):
                result = pipeline.run()
                self.assertEqual((result["rc"], result["verdict"], result["artifact"]),
                                 (3, "CANNOT_SEE", None))
                self.assertEqual(result["reason_codes"], ["TARGET_UNCALIBRATED"])
                self.assertTrue(all(row["verdict"] == "PASS" and row["cache_hit"] == cached
                                    for row in result["stages"]))

    def test_prop_cache_does_not_satisfy_character_prefix(self):
        prop = engine.Pipeline(self.output, self.native).run()
        character = engine.Pipeline(self.output, self.native, plan="character").run()
        first, second = prop["stages"][0], character["stages"][0]
        self.assertEqual((first["rc"], second["rc"], second["cache_hit"]), (0, 0, False))
        self.assertNotEqual(engine.load(first["receipt"]["path"])["key"],
                            engine.load(second["receipt"]["path"])["key"])
        self.assertNotEqual(prop["run_key"], character["run_key"])

    def test_character_refuses_credential_basename_before_reading(self):
        from tools.pipeline.character_backend import CharacterBackend

        pin = core.sha256_json({})
        with self.assertRaises(engine.Refusal) as refused:
            CharacterBackend(self.native, registration={"path": str(self.root / ".netrc"), "sha256": pin},
                             toolchain_sha256=pin, face_runner=None)
        self.assertEqual((refused.exception.rc, refused.exception.reason), (2, "sensitive_path_refused"))


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wire_d.WireCharacterTests.setUpClass()

    def setUp(self):
        self.fixture = wire_d.WireCharacterTests(
            "test_accepted_fixtures_chain_to_reopened_animated_glb")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root = self.fixture.root

    def backend(self, native):
        from tools.pipeline.character_backend import CharacterBackend

        registered = self.fixture.make_runner()
        return CharacterBackend(native, registration=registered.registration,
                                toolchain_sha256=registered.toolchain, face_runner=registered.face_runner)

    def test_accepted_character_chain_and_cache_replay(self):
        native = SyntheticStages(self.root)
        execute = native.execute

        def replay_export(name, directory, subject):
            result = execute(name, directory, subject)
            if name == "export":
                shutil.copyfile(self.fixture.skin["path"], directory / result["subject"])
            return result

        native.execute = replay_export
        native.parameters["export"]["scope"] = "registered-skin-fixture"
        output = self.root / "pipeline"
        backend = self.backend(native)
        pipeline = engine.Pipeline(output, backend, plan="character")
        for cached in (False, True):
            with self.subTest(cached=cached):
                run = pipeline.run()
                self.assertEqual((run["rc"], run["verdict"], run["artifact"]),
                                 (3, "CANNOT_SEE", None), run)
                self.assertEqual(run["reason_codes"], ["TARGET_UNCALIBRATED"])
                self.assertEqual(tuple(row["stage"] for row in run["stages"]),
                    ("input", "provider", "mesh", "topology", "uv_bake_orm", "export",
                     "face", "skin", "act", "voice", "phys", "appearance", "vqa3d"))
                previous = None
                for row in run["stages"]:
                    engine.authenticate(row["receipt"], inside=output)
                    receipt = engine.load(row["receipt"]["path"])
                    self.assertEqual(receipt["previous"], previous)
                    self.assertEqual((row["rc"], row["verdict"], row["cache_hit"]), (0, "PASS", cached))
                    if row["stage"] in ("face", "skin", "act", "voice", "phys"):
                        self.assertEqual(receipt["parameters"]["production_verdict"], "CANNOT_SEE")
                        self.assertIn(backend.registration, receipt["inputs"])
                    previous = row["receipt"]
                if not cached:
                    first_receipts = [row["receipt"] for row in run["stages"]]
                else:
                    self.assertEqual([row["receipt"] for row in run["stages"]], first_receipts)

    def test_character_refuses_an_export_for_another_candidate(self):
        native = SyntheticStages(self.root)
        output = self.root / "pipeline"
        backend = self.backend(native)
        run = engine.Pipeline(output, backend, plan="character").run()
        face = next(row for row in run["stages"] if row["stage"] == "face")
        receipt = engine.load(face["receipt"]["path"])
        self.assertEqual((face["rc"], receipt["reason_codes"]), (4, ["registered_candidate_stale"]))
        self.assertIsNone(run["artifact"])
        self.assertEqual(native.calls, list(engine.STAGES[:6]))
        backend.bind_prefix("face", tuple(row["receipt"] for row in run["stages"][:6]))
        with self.assertRaises(engine.Refusal) as refused:
            backend.identity("face", Path(self.fixture.skin["path"]))
        self.assertEqual((refused.exception.rc, refused.exception.reason),
                         (4, "export_candidate_stale"))
