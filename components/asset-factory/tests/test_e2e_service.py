"""Synthetic service through the REAL scheduler and adapter, without a model."""
import functools
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asset_factory import core
from tools.pipeline import engine, stages
from providers.trellis2 import admission
from test_f1u2_adapter import FakeService


class Lifecycle:
    def __init__(self):
        self.resident, self.stopped, self.samples = None, False, 0

    async def observe(self, handle, deadline_ns):
        self.samples += 1
        return {"schema": "af.scheduler-observation/v1", "sample_id": "synthetic-" + str(self.samples),
                "monotonic_ns": time.monotonic_ns(), "host_mem_available_bytes": 4 * 2**30,
                "cgroup_headroom_bytes": 4 * 2**30, "effective_ceiling_bytes": 8 * 2**30,
                "swap_limit_bytes": 0, "residents": [self.resident] if self.resident else [],
                "absence_reasons": {}}

    async def start(self, request, deadline_ns):
        self.resident = {"handle": "owned", "provider_id": request["provider_id"],
                         "provider_revision": request["provider_revision"]}
        return "owned"

    async def stop(self, handle, deadline_ns):
        self.resident, self.stopped = None, True


class ServiceBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("AF_E2E_OUTPUT"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "reference.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-only input gate stand-in")
        self.provider = {"schema": "af.provider/v1", "id": "trellis2", "slot": "generate", "kind": "model",
            "runnable": True, "revision": "a" * 40, "licence": {"code": "MIT", "weights": "MIT", "deps": []},
            "territory_excluded": [], "commercial": True, "gated": False,
            "weights": [{"repo": "synthetic/model", "revision": "b" * 40,
                         "files": [{"path": "fixture.bin", "sha256": "c" * 64}]}],
            "runtime": {"container": "synthetic-only", "ram_peak_gb_measured": 1,
                        "ram_peak_measurement_ref": {"path": "synthetic-measurement.json", "sha256": "d" * 64}},
            "known_issues": []}
        # The synthetic service needs a workload-bound calibration witness too.
        # Relocate evidence to scratch; keep the real calibration verifier.
        self.calibration = self.root / "synthetic-measurement.json"
        core.atomic_write_json(self.calibration, {
            "scope": admission.SCOPE, "kind": "GB10 native geometry calibration",
            "ram_peak_gib_rounded_up": 1,
            "runs": [{"data": {"pipeline_type": "512", "geometry_only": True,
                                "preprocess_image": False, "rc": 0,
                                "cgroup_peak_bytes": 2**30}}]})
        self.provider["runtime"]["ram_peak_measurement_ref"]["sha256"] = core.sha256_file(self.calibration)
        verifier = mock.patch.object(admission, "calibrated_peak", functools.partial(
            admission.calibrated_peak, provider_path=self.root / "provider_manifest.json"))
        verifier.start()
        self.addCleanup(verifier.stop)
        config = {"schema": "af.factory-config/v1", "jurisdiction": "ES", "commercial_use": False,
            "models_path": "synthetic", "memory": {"max_resident_models": 1,
                "ceiling_gb_default": 8, "min_free_gb_before_start": 2},
            "slots": {s: {"provider": "trellis2" if s == "generate" else "none"} for s in core.SLOTS},
            "gates": {"appearance": {"floor": 4, "target": 7}, "vqa3d_profile": "synthetic"}}
        policy = core.load_json(ROOT / "config/territory-groups.v1.json", max_bytes=65536)
        self.opts = {"timeout_s": 1, "seed": 42, "pipeline_type": "512"}
        for name, value in (("provider_manifest", self.provider), ("factory_config", config), ("territory_policy", policy)):
            path = self.root / (name + ".json")
            core.atomic_write_json(path, value)
            self.opts[name] = path

    def backend(self, service, lifecycle=None):
        return stages.NativeStages(source=self.image, reference=self.image, provider="trellis2", options=self.opts,
                                   service=service, scheduler_backend=lifecycle)

    def test_real_adapter_is_run_under_scheduler_and_release_is_observed(self):
        service, lifecycle = FakeService(), Lifecycle()
        service.provider = self.provider
        directory = self.root / "success"
        directory.mkdir()
        result = self.backend(service, lifecycle).stage_provider(directory, None)
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(service.calls, 1)
        self.assertTrue(lifecycle.stopped)
        self.assertFalse((directory / "model.lease").exists())
        self.assertTrue((directory / "raw.glb").is_file())
        self.assertEqual(engine.load(directory / "scheduler.json")["cleanup_rc"], 0)

    def test_actual_provider_timeout_releases_and_cannot_publish(self):
        service, lifecycle = FakeService(delay=2), Lifecycle()
        service.provider = self.provider
        directory = self.root / "timeout"
        directory.mkdir()
        with self.assertRaises(engine.Refusal) as raised:
            self.backend(service, lifecycle).stage_provider(directory, None)
        self.assertIn(raised.exception.rc, (3, 6))
        self.assertEqual(raised.exception.evidence_record["operation_rc"], 6)
        self.assertEqual(raised.exception.evidence_record["cleanup_rc"], 0)
        self.assertTrue(lifecycle.stopped)
        self.assertFalse((directory / "raw.glb").exists())
        self.assertFalse((directory / "model.lease").exists())

    def test_transport_without_lifecycle_refuses_before_model_start(self):
        service = FakeService()
        directory = self.root / "no-lifecycle"
        directory.mkdir()
        with self.assertRaises(engine.Refusal) as raised:
            self.backend(service).stage_provider(directory, None)
        self.assertEqual(raised.exception.rc, 3)
        self.assertEqual(service.calls, 0)

    def test_missing_calibration_refuses_before_model_start(self):
        service, lifecycle = FakeService(), Lifecycle()
        service.provider = self.provider
        self.calibration.unlink()
        directory = self.root / "unmeasured"
        directory.mkdir()
        with self.assertRaises(engine.Refusal) as raised:
            self.backend(service, lifecycle).stage_provider(directory, None)
        self.assertEqual(raised.exception.rc, 3)
        self.assertIn("peak_unmeasured", raised.exception.evidence_record["reason_codes"])
        self.assertEqual(service.calls, 0)
        self.assertIsNone(lifecycle.resident)
        self.assertFalse((directory / "generation/raw.glb").exists())

    def test_licence_implementation_is_part_of_provider_cache_identity(self):
        backend = self.backend(None)
        before = backend._pin("provider")
        original = core.sha256_file
        def changed(path):
            return "e" * 64 if Path(path) == ROOT / "licence/check.py" else original(path)
        with mock.patch.object(core, "sha256_file", changed):
            self.assertNotEqual(backend._pin("provider"), before)

    def test_stage_tuple_rc_mismatch_is_an_error(self):
        with self.assertRaises(engine.Refusal) as raised:
            stages.checked({"rc": 0, "status": "OK"}, declared_rc=5)
        self.assertEqual(raised.exception.rc, 2)

    def test_unproven_release_retains_global_lease_and_rejected_raw(self):
        class BrokenRelease(Lifecycle):
            async def stop(self, handle, deadline_ns):
                raise RuntimeError("synthetic release failure")
        class TestInput(stages.NativeStages):
            scope = "synthetic-image-gate-test"
            def stage_input(self, directory, subject):
                core.atomic_write_json(directory / "test-input.json", {"synthetic": True})
                return stages.success(None, ["test-input.json"], ["test-input.json"])
        service, lifecycle = FakeService(), BrokenRelease()
        service.provider = self.provider
        backend = TestInput(source=self.image, reference=self.image, provider="trellis2", options=self.opts,
                            service=service, scheduler_backend=lifecycle)
        out = self.root / "failed-release"
        first = engine.Pipeline(out, backend).run()
        self.assertEqual(first["rc"], 3, first)
        self.assertTrue((out / "state/trellis2.lease").is_file())
        self.assertTrue(list((out / "rejected").glob("provider-*/generation/raw.glb")))
        second = engine.Pipeline(out, backend).run()
        self.assertEqual(second["stages"][1]["rc"], 5, second)
        self.assertEqual(service.calls, 1)


if __name__ == "__main__":
    unittest.main()
