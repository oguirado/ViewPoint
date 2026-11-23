"""SAT BYO custody, distinct bake sources and the protected LOOK handoff."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.blender import bake, look_bake
from tools.pipeline import engine, stages

import test_n18_lookbake_materials as look
from test_e2e_blender import options


class WireA6(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get("WIRE_A6_EVIDENCE_DIR")
        if evidence:
            self.root = Path(evidence) / self._testMethodName
            self.root.mkdir(parents=True)
        else:
            temporary = TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            self.root = Path(temporary.name)
        self.high = look.sheet((0, -39322, 52429))
        self.low, store, self.witness, _, _ = look.fixture(
            high=self.high, relief=True, measured_orm=False)
        self.source, self.target = self.root / "raw.glb", self.root / "topology.glb"
        self.source.write_bytes(self.high)
        self.target.write_bytes(self.low)
        self.reference = self.root / "reference.png"
        self.reference.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.options = options(self.root)
        self.authority = self.root / "authority"
        for (role, pin), data in store.records.items():
            path = self.authority / role / pin[7:]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.witness_file = self.authority / "look-bake-witness" / self.witness[7:]
        policy = engine.load(self.options["mesh_policy"])
        policy.update(allow_open=True, component_allowlist={"sheet": 1})
        policy["geometry_scope"] = {"authority": str(self.authority),
                                    "look_scope": {"witness_digest": self.witness}}
        core.atomic_write_json(self.options["mesh_policy"], policy)

    def backend(self, provider="byo", cls=stages.NativeStages):
        return cls(source=self.source, reference=self.reference, provider=provider, options=self.options)

    def test_sat_byo_through_protected_uv_bake(self):
        # The planar SAT relief fixture supplies the topology handoff. Its LOOK
        # geometry receipt measures and pins both meshes. A side silhouette of
        # this sheet is empty, so use the UV/bake boundary directly.
        native = self.backend()
        provider = self.root / "provider"
        provider.mkdir()
        raw = provider / native.stage_provider(provider, None)["subject"]
        custody = engine.load(provider / "custody.json")
        self.assertEqual(custody["input_sha256"], core.sha256_file(raw))
        native.bind_raw(raw)
        target = self.root / "stage"
        target.mkdir()
        refs, parameters, _ = native.identity("uv_bake_orm", self.target)
        receipt = native.stage_uv_bake_orm(target, self.target)
        high_sha, low_sha = core.sha256_file(raw), core.sha256_file(self.target)
        self.assertNotEqual(high_sha, low_sha)
        self.assertEqual(core.sha256_file(target / "bake/source_high.glb"), high_sha)
        self.assertEqual(core.sha256_file(target / "bake/source_low.glb"), low_sha)
        verified = engine.load(target / "look-verification.json")
        self.assertEqual(verified["scope"], "protected-look-replay")
        self.assertEqual(verified["look_witness_digest"], self.witness)
        self.assertFalse(verified["production_evidence"])
        self.assertEqual(parameters["production_verdict"], "CANNOT_SEE")
        witness = engine.load(self.witness_file)
        look_receipt = engine.load(target / "bake/look_receipt.json")
        self.assertEqual(look_receipt["normal_probe"]["expected_y_sign"], 1)
        images = look_bake.read_look_maps((target / "bake/textured.glb").read_bytes(), self.low)
        pixel = look_bake.decode_look_png(images["normal"], look.SIZE)
        green = pixel[4 * look.RELIEF_TEXEL + 1]
        self.assertGreater(green, 128)
        self.assertIn(witness["projection_digest"][7:], {ref["sha256"] for ref in refs})
        native.verify_sources()
        core.atomic_write_json(self.root / "result.json", {
            "stage_rc": receipt["rc"], "production_verdict": parameters["production_verdict"],
            "source_high_sha256": high_sha, "source_low_sha256": low_sha,
            "transferred_normal_green": green, "verification": verified,
        })

        # Mutate a transitive registration after verification. The final
        # verify_sources gate must still reauthenticate the same byte witness.
        projection = engine.load(self.authority / "uv-projection-layout" / witness["projection_digest"][7:])
        registration = self.authority / "uv-source-confidence" / projection["views"]["front"]["confidence_digest"][7:]
        original = registration.read_bytes()
        try:
            registration.write_bytes(original + b"\0")
            with self.assertRaisesRegex(engine.Refusal, "^bytes_stale$") as refusal:
                native.verify_sources()
            self.assertEqual(refusal.exception.rc, 4)
        finally:
            registration.write_bytes(original)

    def test_production_without_look_refuses_before_bake(self):
        policy = engine.load(self.options["mesh_policy"])
        del policy["geometry_scope"]["look_scope"]
        core.atomic_write_json(self.options["mesh_policy"], policy)
        native = self.backend(provider="trellis2")
        native.bind_raw(self.source)
        with patch.object(bake, "bake_bundle", return_value={"rc": 2}) as launch:
            with self.assertRaisesRegex(engine.Refusal, "^look_scope_missing$") as refusal:
                native.stage_uv_bake_orm(self.root, self.target)
            self.assertEqual(refusal.exception.rc, 3)
            launch.assert_not_called()

    def test_witness_mismatch_refuses_before_bake(self):
        self.witness_file.write_bytes(self.witness_file.read_bytes() + b" ")
        with patch.object(bake, "bake_bundle", return_value={"rc": 2}) as launch:
            with self.assertRaisesRegex(engine.Refusal, "^look_evidence_stale$") as refusal:
                self.backend().stage_uv_bake_orm(self.root, self.target)
            self.assertEqual(refusal.exception.rc, 4)
            launch.assert_not_called()

    def test_provider_raw_drift_refuses_before_bake(self):
        native = self.backend()
        # Same byte witness the engine passes to bind_raw after BYO custody.
        provider = self.root / "provider"
        provider.mkdir()
        raw = provider / native.stage_provider(provider, None)["subject"]
        native.bind_raw(raw)
        raw.write_bytes(self.low)
        with patch.object(bake, "bake_bundle", return_value={"rc": 2}) as launch:
            with self.assertRaisesRegex(engine.Refusal, "^bytes_stale$") as refusal:
                native.stage_uv_bake_orm(self.root, self.target)
            self.assertEqual(refusal.exception.rc, 4)
            launch.assert_not_called()

    def test_old_self_bake_cache_cannot_supply_new_identity(self):
        native = self.backend()
        refs, parameters, pin = native.identity("uv_bake_orm", self.target)
        legacy = {"stage": "uv_bake_orm", "previous": None, "toolchain_sha256": pin,
                  "inputs": [engine.file_ref(self.options["mesh_policy"]), native.raw_ref,
                             engine.file_ref(self.target)],
                  "parameters": {key: self.options.get(key) for key in
                                 ("timeout_s", "texture_resolution", "max_stretch")}}
        legacy["parameters"]["provider"] = "byo"
        old_key = core.sha256_json(legacy)
        new_key = core.sha256_json({**legacy, "inputs": refs, "parameters": parameters})
        self.assertNotEqual(new_key, old_key)
        old = self.root / "cache" / "uv_bake_orm" / old_key
        old.mkdir(parents=True)
        for name in ("source_high.glb", "source_low.glb"):
            (old / name).write_bytes(self.low)
        self.assertFalse((old.parent / new_key).exists())
        with self.assertRaisesRegex(engine.Refusal, "^bake_inputs_stale$") as refusal:
            native._verify_bake_sources(old, native.raw_ref["sha256"], core.sha256_file(self.target))
        self.assertEqual(refusal.exception.rc, 4)

    def _baked_record(self):
        native = self.backend()
        provider = self.root / "provider-cache"
        provider.mkdir()
        raw = provider / native.stage_provider(provider, None)["subject"]
        native.bind_raw(raw)
        target = self.root / "bake-cache"
        target.mkdir()
        _, parameters, _ = native.identity("uv_bake_orm", self.target)
        self.assertEqual(native.stage_uv_bake_orm(target, self.target)["rc"], 0)
        fresh = self.backend()
        fresh.bind_raw(native.raw_ref["path"])
        fresh.identity("uv_bake_orm", self.target)
        record = {"parameters": parameters, "evidence": [], "subject": str(target / "named_low.glb")}
        return fresh, target, record, parameters

    def _cached_refusal(self, fresh, target, record):
        with self.assertRaises(engine.Refusal) as refusal:
            fresh.verify_cached("uv_bake_orm", target, record)
        return refusal.exception.rc, refusal.exception.reason

    def test_cache_key_records_distinct_high_and_low(self):
        _, _, _, parameters = self._baked_record()
        sources = parameters["bake_sources"]
        self.assertNotEqual(sources["high_sha256"], sources["low_sha256"])

    def test_cache_hit_with_tampered_look_verification_is_stale(self):
        fresh, target, record, _ = self._baked_record()
        verification = target / "look-verification.json"
        data = json.loads(verification.read_bytes())
        data["checks_evaluated"] = data.get("checks_evaluated", 0) + 1
        verification.write_text(json.dumps(data))
        self.assertEqual(self._cached_refusal(fresh, target, record), (4, "look_verification_stale"))

    def test_cache_hit_with_high_replaced_by_low_is_stale(self):
        fresh, target, record, _ = self._baked_record()
        (target / "bake/source_high.glb").write_bytes((target / "bake/source_low.glb").read_bytes())
        self.assertEqual(self._cached_refusal(fresh, target, record), (4, "bake_inputs_stale"))
