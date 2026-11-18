"""Pin the cage-to-UV boundary and reopen the protected export independently."""
import json
from pathlib import Path
import shutil
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.blender import bake, glb_io, look_bake
from tools.input.evidence import bytes_digest
from tools.pipeline import engine, stages, uv_derivative
import test_d18_geo_certificate as geometry
from test_e2e_blender import options
from test_n18_int_geometry import fixture_scope
import test_n18_lookbake_materials as look


class WireUVX(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.high = self.root / "raw.glb"
        self.high.write_bytes(look.sheet((0, -39322, 52429)))
        self.low, self.store, self.witness, _, _ = look.fixture(
            high=self.high.read_bytes(), relief=True, measured_orm=False)
        self.authority = self.root / "authority"
        self.opts = options(self.root)
        self.reference = self.root / "reference.png"
        self.reference.write_bytes(b"\x89PNG\r\n\x1a\n")
        doc, binary = glb_io.decode_glb(self.low)
        primitive = doc["meshes"][0]["primitives"][0]
        primitive["attributes"].pop("TANGENT")
        primitive["attributes"].pop("TEXCOORD_0")
        self.cage_low = self.root / "master.glb"
        self.cage_low.write_bytes(glb_io.encode_glb(doc, binary))
        self.cage = self.cage_low.with_suffix(".cage.json")
        self.cage.write_bytes(b"unit-test cage byte pin")

    def register(self, derivative, *, retarget=False, omit_derivative=False):
        target = self.store.put("uv-derivative-glb", derivative)
        witness = json.loads(self.store.read("look-bake-witness", self.witness))
        if retarget:
            for role, field in (("uv-layout", "atlas_digest"), ("uv-projection-layout", "projection_digest")):
                record = json.loads(self.store.read(role, witness[field]))
                record["mesh_digest"] = target
                witness[field] = self.store.put(role, record)
            witness["mesh_digest"] = target
        witness_pin = self.store.put("look-bake-witness", witness)
        registration = self.store.put("uv-derivative-registration", {
            "schema": "af.uv-derivative-registration/v1",
            "source_digest": bytes_digest(self.cage_low.read_bytes()),
            "cage_digest": bytes_digest(self.cage.read_bytes()),
            "mesh_digest": target, "witness_digest": witness_pin,
            "resolution": self.opts["texture_resolution"],
        })
        scope = {"witness_digest": witness_pin}
        if not omit_derivative:
            scope["uv_derivative_digest"] = registration
            self.opts.update(method="quadriflow", target_faces=3)
        for (role, pin), data in self.store.records.items():
            path = self.authority / role / pin[7:]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        policy = engine.load(self.opts["mesh_policy"])
        policy["geometry_scope"] = {"authority": str(self.authority), "look_scope": scope}
        core.atomic_write_json(self.opts["mesh_policy"], policy)
        return stages.NativeStages(source=self.high, reference=self.reference, provider="byo", options=self.opts)

    def _backend_with_geometry(self, **candidates):
        geo = geometry.Gates()
        geo.setUp()
        geo.raw = self.high.read_bytes()
        geo.policy.update(source=bytes_digest(geo.raw), reference_length_um=900000, face_budgets=[3])
        geo.policy["shells"]["0:0"].update(loops=1, orientation=0)
        geo.intent["extent_ranges_um"] = [[900000, 2000000], [0, 2000000], [0, 2000000]]
        geo.policy["intent"] = geo.store.add("raw-intent", geo.intent)
        geo.patches = {"face": ["0:0", [0]], "left_hand": ["0:0", [1]], "right_hand": ["0:0", [2]]}
        policy = engine.load(self.opts["mesh_policy"])
        for name, candidate in candidates.items():
            policy["geometry_scope"][name] = fixture_scope(geo, self.authority, candidate)
        core.atomic_write_json(self.opts["mesh_policy"], policy)
        native = stages.NativeStages(source=self.high, reference=self.reference, provider="byo", options=self.opts)
        native.bind_output(self.root / "pipeline")
        return native

    def _bind_uv_geometry(self, native, directory, result):
        candidate = directory / result["subject"]
        proof = native._geometry_proof("uv_bake_orm", candidate)
        path = directory / stages.GEOMETRY_RECEIPT
        core.atomic_write_json(path, proof)
        result["evidence"].append(path.name)
        return {"subject": str(candidate), "evidence": [engine.file_ref(path)]}

    def test_resealed_derivative_changes_geometry(self):
        doc, raw = glb_io.decode_glb(self.low)
        buffer = bytearray(raw)
        primitive = doc["meshes"][0]["primitives"][0]
        position = doc["accessors"][primitive["attributes"]["POSITION"]]
        view = doc["bufferViews"][position["bufferView"]]
        offset = view.get("byteOffset", 0) + position.get("byteOffset", 0)
        struct.pack_into("<f", buffer, offset, 0.125)
        native = self.register(glb_io.encode_glb(doc, bytes(buffer)), retarget=True)
        with patch.object(bake, "bake_bundle") as launch:
            with self.assertRaisesRegex(uv_derivative.Refusal, "^uv_derivative_geometry_drift$") as raised:
                native.stage_uv_bake_orm(self.root, self.cage_low)
            self.assertEqual(raised.exception.rc, 4)
            launch.assert_not_called()

    def test_another_pair_refuses_before_preparation_or_bake(self):
        native = self.register(self.cage_low.read_bytes())
        with (patch.object(uv_derivative, "prepare") as prepare,
              patch.object(bake, "bake_bundle") as launch,
              self.assertRaisesRegex(engine.Refusal, "^look_subject_drift$") as raised):
            native.stage_uv_bake_orm(self.root, self.cage_low)
        self.assertEqual(raised.exception.rc, 4)
        prepare.assert_not_called()
        launch.assert_not_called()

    def test_underived_cache_does_not_match_registered_key(self):
        native = self.register(self.low)
        refs, params, toolchain = native.identity("uv_bake_orm", self.cage_low)
        self.assertEqual(params["bake_sources"]["low_sha256"], bytes_digest(self.low)[7:])
        self.assertIn(engine.file_ref(self.cage), refs)
        key = {"stage": "uv_bake_orm", "inputs": refs, "parameters": params, "toolchain_sha256": toolchain}
        previous = {field: value for field, value in params.items() if field != "uv_derivative"}
        previous["bake_sources"] = {**previous["bake_sources"], "low_sha256": core.sha256_file(self.cage_low)}
        self.assertNotEqual(core.sha256_json(key), core.sha256_json({**key, "parameters": previous}))

        bundle = self.root / "bake"
        bundle.mkdir()
        (bundle / "source_high.glb").write_bytes(self.high.read_bytes())
        (bundle / "source_low.glb").write_bytes(self.cage_low.read_bytes())
        with self.assertRaisesRegex(engine.Refusal, "^uv_derivative_cache_stale$") as raised:
            native.verify_cached("uv_bake_orm", self.root, {
                "parameters": previous, "inputs": refs, "toolchain_sha256": toolchain})
        self.assertEqual(raised.exception.rc, 4)

    def test_active_node_matrix_and_trs_are_refused(self):
        for field, value in {
            "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 1],
            "translation": [1, 0, 0], "rotation": [0, 0, 1, 0], "scale": [2, 1, 1],
        }.items():
            with self.subTest(field=field):
                doc, binary = glb_io.decode_glb(self.low)
                doc["nodes"][0][field] = value
                with self.assertRaisesRegex(uv_derivative.Refusal, "^nonidentity_glb_node_transform$"):
                    uv_derivative.same_geometry(self.cage_low.read_bytes(),
                                                glb_io.encode_glb(doc, binary), max_faces=3)

    def test_prepare_with_owned_blender(self):
        output = self.root / "prepared.glb"
        with patch.object(bake, "_look_scoped_runner", wraps=bake._look_scoped_runner) as runner:
            raw = uv_derivative.prepare(self.cage_low, output, resolution=64, timeout_s=90)
        runner.assert_called_once()
        self.assertEqual(raw, output.read_bytes())
        self.assertEqual(uv_derivative.same_geometry(self.cage_low.read_bytes(), raw, max_faces=3), 3)

    def test_prepare_refuses_missing_exporter_precision_setting(self):
        output = self.root / "prepared.glb"
        owned_runner = bake._look_scoped_runner

        def run(argv, timeout):
            index = argv.index("--python")
            expression = (
                "import runpy; "
                "from io_scene_gltf2.blender.exp import gltf2_blender_gather_primitives_extract as exporter; "
                f"del exporter.ROUNDING_DIGIT; runpy.run_path({argv[index + 1]!r}, run_name='__main__')")
            return owned_runner([*argv[:index], "--python-expr", expression, *argv[index + 2:]], timeout)

        with (patch.object(bake, "_look_scoped_runner", side_effect=run),
              self.assertRaisesRegex(uv_derivative.Refusal, "^uv_derivative_worker_unavailable$") as raised):
            uv_derivative.prepare(self.cage_low, output, resolution=64, timeout_s=90)
        self.assertEqual(raised.exception.rc, 3)
        self.assertFalse(output.exists())

    def _fixture_unwrap(self, argv, timeout, *, raw=None):
        self.assertEqual(argv[argv.index("--python") + 1], str(Path(uv_derivative.__file__).resolve()))
        self.assertEqual(argv[-3:], [str(self.cage_low), argv[-2], "64"])
        self.assertEqual(timeout, self.opts["timeout_s"])
        Path(argv[-2]).write_bytes(self.low if raw is None else raw)
        return 0, "AF_UV_DERIVATIVE_READY\n", ""

    def test_fresh_registered_uv_bake_and_cache_replay(self):
        self.register(self.low)
        native = self._backend_with_geometry(uv_bake_orm=self.low)
        refs, params, pin = native.identity("uv_bake_orm", self.cage_low)
        owned_runner = bake._look_scoped_runner

        def run(argv, timeout):
            if str(Path(uv_derivative.__file__).resolve()) in argv:
                return self._fixture_unwrap(argv, timeout)
            return owned_runner(argv, timeout)

        # This fixture supplies the registered atlas at the unwrap worker boundary.
        # The preceding prepare test runs the actual unwrap worker independently.
        with patch.object(bake, "_look_scoped_runner", side_effect=run):
            result = native.stage_uv_bake_orm(self.root, self.cage_low)
        self.assertEqual(result["rc"], 0)
        self.assertEqual((self.root / result["subject"]).read_bytes(), self.low)
        self.assertEqual((self.root / "prepared_low.glb").read_bytes(), self.low)
        self.assertEqual(engine.load(self.root / "uv-derivative.json"), params["uv_derivative"])
        record = {"inputs": refs, "parameters": params, "toolchain_sha256": pin,
                  **self._bind_uv_geometry(native, self.root, result)}
        with patch.object(uv_derivative, "prepare") as prepare, patch.object(bake, "bake_bundle") as launch:
            native.verify_cached("uv_bake_orm", self.root, record)
            with self.subTest(case="changed_derivative_toolchain"):
                self._assert_changed_toolchain_refuses(native, "uv_bake_orm", self.root, record)
            (self.root / "prepared_low.glb").write_bytes(self.cage_low.read_bytes())
            with self.assertRaisesRegex(engine.Refusal, "^uv_derivative_cache_stale$") as caught:
                native.verify_cached("uv_bake_orm", self.root, record)
            self.assertEqual(caught.exception.rc, 4)
            prepare.assert_not_called()
            launch.assert_not_called()

    def test_fresh_uv_reproduction_digest_refuses(self):
        native = self.register(self.low)
        doc, binary = glb_io.decode_glb(self.low)
        doc["asset"]["extras"] = {"fixture": "same geometry, different bytes"}
        other = glb_io.encode_glb(doc, binary)
        with (patch.object(bake, "_look_scoped_runner", side_effect=lambda argv, timeout:
                           self._fixture_unwrap(argv, timeout, raw=other)),
              patch.object(bake, "bake_bundle", return_value={"rc": 3}) as launch):
            with self.assertRaisesRegex(engine.Refusal, "^uv_derivative_reproduction_drift$") as caught:
                native.stage_uv_bake_orm(self.root, self.cage_low)
            self.assertEqual(caught.exception.rc, 4)
            launch.assert_not_called()

    def _assert_changed_toolchain_refuses(self, native, name, directory, record):
        worker = Path(uv_derivative.__file__).resolve()
        isolated = self.root / worker.name
        raw = worker.read_bytes()
        middle = raw.index(b"atlas")
        isolated.write_bytes(raw[:middle] + b"A" + raw[middle + 1:])
        real_sha = core.sha256_file
        with patch.object(core, "sha256_file", side_effect=lambda path:
                          real_sha(isolated if path == worker else path)):
            self.assertNotEqual(native._pin(name), record["toolchain_sha256"])
            with self.assertRaisesRegex(engine.Refusal, "^upstream_toolchain_stale$") as caught:
                native.verify_cached(name, directory, record)
            self.assertEqual(caught.exception.rc, 4)

    def test_protected_export_reopens_exact_bundle_and_rejects_repeat(self):
        native = self.register(self.low, omit_derivative=True)
        target = self.root / "baked"
        target.mkdir()
        result = native.stage_uv_bake_orm(target, self.authority / "uv-derivative-glb" /
                                         bytes_digest(self.low)[7:])
        self.assertEqual(result["rc"], 0)
        out = self.root / "export"
        out.mkdir()
        subject = target / result["subject"]
        export = native.stage_export(out, subject)
        self.assertEqual(export["rc"], 0)
        candidate = out / export["subject"]
        original = (target / "bake/textured.glb").read_bytes()
        self.assertEqual(candidate.read_bytes(), original)
        binding = engine.load(out / "protected-export.json")
        maps = look_bake.read_look_maps(candidate.read_bytes(), self.low)
        self.assertEqual(binding["map_digests"], {role: bytes_digest(raw) for role, raw in maps.items()})
        self.assertFalse(binding["production_evidence"])
        document, binary = glb_io.decode_glb(original)
        document["samplers"][0].update(wrapS=10497, wrapT=10497)
        candidate.write_bytes(glb_io.encode_glb(document, binary))
        scope, _, _ = native._bake_look_scope()
        with self.assertRaisesRegex(uv_derivative.Refusal, "^look_export_bundle_drift$") as stale:
            native._protected_export_binding(candidate, subject, scope)
        self.assertEqual(stale.exception.rc, 4)
        with self.assertRaisesRegex(look_bake.Refusal, "^look_material_or_sampler_drift$") as raised:
            look_bake.read_look_maps(candidate.read_bytes(), self.low)
        self.assertEqual(raised.exception.rc, 5)

    def test_engine_protected_export_cache_replay_and_negatives(self):
        native = self.register(self.low, omit_derivative=True)
        upstream = self.authority / "uv-derivative-glb" / bytes_digest(self.low)[7:]
        baked = self.root / "baked"
        baked.mkdir()
        result = native.stage_uv_bake_orm(baked, upstream)
        original = (baked / "bake" / "textured.glb").read_bytes()
        native = self._backend_with_geometry(uv_bake_orm=self.low, export=original)
        self._bind_uv_geometry(native, baked, result)
        pipeline = engine.Pipeline(self.root / "pipeline", native)
        pipeline.stages = ("uv_bake_orm", "export")
        identity, execute, verify = native.identity, native.execute, native.verify_cached

        def inputs(name, subject):
            refs, params, pin = identity(name, upstream if name == "uv_bake_orm" else subject)
            if name == "export":
                refs = [ref for ref in refs if ref != native.raw_ref] + [native.raw_ref]
            return refs, params, pin

        def run_stage(name, directory, subject):
            if name == "uv_bake_orm":
                shutil.copytree(baked, directory, dirs_exist_ok=True)
                return result
            # The planar fixture has an empty edge-on silhouette in the legacy
            # four-view gate. The native scoped geometry proof still runs.
            with (patch.object(stages.mesh, "compare_meshes", return_value=(0, {})),
                  patch.object(stages.topology_bridge, "verify_regression")):
                return execute(name, directory, subject)

        with (patch.object(native, "identity", side_effect=inputs),
              patch.object(native, "execute", side_effect=run_stage) as execution):
            fresh = pipeline.run()["stages"][-1]
            original_record = engine.load(fresh["receipt"]["path"])
            self.assertEqual((fresh["rc"], fresh["cache_hit"]), (0, False), original_record["reason_codes"])
            candidate = Path(original_record["subject"])
            self.assertEqual(candidate.read_bytes(), original)
            self.assertEqual(original_record["inputs"][-1], native.raw_ref)
            execution.reset_mock()

            def replay(name, directory, record):
                if name != "export":
                    return verify(name, directory, record)
                if case == "relative_subject":
                    return verify(name, directory, {**record, "subject": candidate.name})
                if case == "wrong_input":
                    return verify(name, directory, {**record, "inputs": [
                        ref for ref in record["inputs"] if Path(ref["path"]).name != "named_low.glb"]})
                if case == "tampered_after_byte_check":
                    try:
                        candidate.write_bytes(tampered)
                        return verify(name, directory, record)
                    finally:
                        candidate.write_bytes(original)
                return verify(name, directory, record)

            # Add aligned JSON padding so only the byte binding can see this change.
            json_size, = struct.unpack_from("<I", original, 12)
            json_end = 20 + json_size
            tampered = (original[:8] + struct.pack("<II", len(original) + 4, json_size + 4)
                        + original[16:json_end] + b"    " + original[json_end:])
            self.assertEqual(glb_io.decode_glb(tampered), glb_io.decode_glb(original))
            for case, expected in (
                ("absolute_subject", (0, True, [])),
                ("relative_subject", (0, True, [])),
                ("wrong_input", (4, False, ["look_export_input_stale"])),
                ("tampered_after_byte_check", (4, False, ["look_export_bundle_drift"])),
            ):
                with self.subTest(case=case), patch.object(native, "verify_cached", side_effect=replay):
                    row = pipeline.run()["stages"][-1]
                    record = engine.load(row["receipt"]["path"])
                    self.assertEqual((row["rc"], row["cache_hit"], record["reason_codes"]), expected)
                    if row["cache_hit"]:
                        self.assertEqual(row["receipt"], fresh["receipt"])
            with self.subTest(case="changed_derivative_toolchain"):
                self._assert_changed_toolchain_refuses(native, "export", candidate.parent, original_record)
            candidate.write_bytes(tampered)
            stale = pipeline.run()["stages"][-1]
            record = engine.load(stale["receipt"]["path"])
            self.assertEqual((stale["rc"], stale["cache_hit"], record["reason_codes"]),
                             (4, False, ["bytes_stale"]))
            execution.assert_not_called()

    def _fixture_cage_backend(self, *, cage=80000):
        self.register(self.low, omit_derivative=True)
        policy = engine.load(self.opts["mesh_policy"])
        scope = policy["geometry_scope"]["look_scope"]
        scope["fixture_cage_policy"] = {"cage_extrusion_um": cage, "production_evidence": False,
                                        "witness_digest": scope["witness_digest"]}
        path = self.root / f"fixture-cage-{cage}.json"
        core.atomic_write_json(path, policy)
        native = stages.NativeStages(source=self.high, reference=self.reference, provider="byo",
                                    options={**self.opts, "mesh_policy": path})
        return native, self.authority / "uv-derivative-glb" / bytes_digest(self.low)[7:]

    def test_refuses_unbound_or_out_of_bounds_fixture_cage(self):
        configured, subject = self._fixture_cage_backend()
        policy = engine.load(configured.policies["mesh_policy"])
        scope = policy["geometry_scope"]["look_scope"]
        valid = scope["fixture_cage_policy"]
        cases = (
            ("out_of_bounds", {"fixture_cage_policy": {**valid, "cage_extrusion_um": 80001}}),
            ("non_integer", {"fixture_cage_policy": {**valid, "cage_extrusion_um": "80000"}}),
            ("unbound", {"fixture_cage_policy": {**valid, "witness_digest": "sha256:" + "0" * 64}}),
            ("production", {"fixture_cage_policy": {**valid, "production_evidence": True}}),
            ("unknown_field", {"fixture_cage_policy": {**valid, "extra": True}}),
            ("bare_override", {"cage_extrusion_um": 80000}),
        )
        for label, fields in cases:
            with self.subTest(case=label):
                policy["geometry_scope"]["look_scope"] = {"witness_digest": scope["witness_digest"], **fields}
                core.atomic_write_json(self.opts["mesh_policy"], policy)
                native = stages.NativeStages(source=self.high, reference=self.reference,
                                            provider="byo", options=self.opts)
                with (patch.object(bake, "bake_bundle") as launch,
                      patch.object(uv_derivative, "prepare") as prepare,
                      self.assertRaisesRegex(engine.Refusal, "^look_fixture_cage_refused$") as refusal):
                    native.stage_uv_bake_orm(self.root, subject)
                self.assertEqual(refusal.exception.rc, 2)
                launch.assert_not_called()
                prepare.assert_not_called()

    def test_refuses_fixture_cage_below_lower_bound(self):
        native, subject = self._fixture_cage_backend(cage=4999)
        with self.assertRaisesRegex(engine.Refusal, "^look_fixture_cage_refused$") as refusal:
            native._prepared_bake_inputs(subject)
        self.assertEqual(refusal.exception.rc, 2)

    def test_fresh_look_refuses_non_object_manifest_policy(self):
        native, subject = self._fixture_cage_backend()
        bundle = self.root / "bake"
        bundle.mkdir()
        (bundle / "source_high.glb").write_bytes(self.high.read_bytes())
        (bundle / "source_low.glb").write_bytes(subject.read_bytes())
        manifest = bundle / "bundle_receipt.json"

        for value in (None, [], "invalid", False, 0):
            with self.subTest(policy=value):
                core.atomic_write_json(manifest, {"runner": {"kind": "scoped-cpu"}, "policy": value})
                receipt = {"rc": 0, "runtime_measured": True, "receipt_sha256": core.sha256_file(manifest)}
                with (patch.object(bake, "bake_bundle", return_value=receipt),
                      self.assertRaisesRegex(engine.Refusal, "^look_manifest_policy_invalid$") as refusal):
                    native.stage_uv_bake_orm(self.root, subject)
                self.assertEqual(refusal.exception.rc, 2)
                self.assertFalse((self.root / "named_low.glb").exists())

    def test_cached_look_refuses_non_object_manifest_policy(self):
        native, subject = self._fixture_cage_backend()
        refs, params, pin = native.identity("uv_bake_orm", subject)
        bundle = self.root / "bake"
        bundle.mkdir()
        (bundle / "source_high.glb").write_bytes(self.high.read_bytes())
        (bundle / "source_low.glb").write_bytes(subject.read_bytes())
        record = {"inputs": refs, "parameters": params, "toolchain_sha256": pin}

        for value in (None, [], "invalid", False, 0):
            with self.subTest(policy=value):
                manifest = bundle / "bundle_receipt.json"
                core.atomic_write_json(manifest, {"policy": value})
                core.atomic_write_json(self.root / "bake.json",
                                       {"receipt_sha256": core.sha256_file(manifest)})
                with self.assertRaisesRegex(engine.Refusal, "^look_manifest_policy_invalid$") as refusal:
                    native.verify_cached("uv_bake_orm", self.root, record)
                self.assertEqual(refusal.exception.rc, 2)

    def test_protected_export_refuses_non_object_manifest_policy(self):
        native, subject = self._fixture_cage_backend()
        scope = native._bake_look_scope()[0]
        named_low = self.root / "named_low.glb"
        named_low.write_bytes(subject.read_bytes())
        bundle = self.root / "bake"
        bundle.mkdir()
        (bundle / "source_high.glb").write_bytes(self.high.read_bytes())
        (bundle / "source_low.glb").write_bytes(subject.read_bytes())

        for value in (None, [], "invalid", False, 0):
            with self.subTest(policy=value):
                manifest = bundle / "bundle_receipt.json"
                core.atomic_write_json(manifest, {"policy": value})
                replay = {"receipt_sha256": core.sha256_file(manifest)}
                core.atomic_write_json(self.root / "bake.json", replay)
                core.atomic_write_json(self.root / "look-verification.json", replay)
                with (patch.object(native, "_verify_bake_look", return_value=replay),
                      self.assertRaisesRegex(engine.Refusal, "^look_manifest_policy_invalid$") as refusal):
                    native._protected_export_binding(bundle / "textured.glb", named_low, scope)
                self.assertEqual(refusal.exception.rc, 2)

    def test_refuses_bake_manifest_with_wrong_effective_cage(self):
        native, subject = self._fixture_cage_backend()
        refs, params, pin = native.identity("uv_bake_orm", subject)
        self.assertEqual(params.get("cage_extrusion_um"), 80000)
        default, _ = self._fixture_cage_backend(cage=5000)
        self.assertEqual(default.identity("uv_bake_orm", subject)[1].get("cage_extrusion_um"), 5000)
        target = self.root / "bake"
        result = bake.bake_bundle(self.high, subject, target, res=self.opts["texture_resolution"],
                                  margin_px=2, blender_threads=1,
                                  look_scope={key: native._bake_look_scope()[0][key]
                                              for key in ("witness_digest", "read_checked")})
        self.assertEqual(result["rc"], 0, result)
        with self.subTest(boundary="fresh_bake"), patch.object(bake, "bake_bundle", return_value=result) as launch:
            with self.assertRaisesRegex(engine.Refusal, "^look_fixture_cage_replay_stale$"):
                native.stage_uv_bake_orm(self.root, subject)
            self.assertEqual(launch.call_args.kwargs.get("cage_extrusion_um"), 80000)

        core.atomic_write_json(self.root / "bake.json", result)
        record = {"inputs": refs, "parameters": params, "toolchain_sha256": pin}
        with (self.subTest(boundary="cached_manifest"),
              self.assertRaisesRegex(engine.Refusal, "^look_fixture_cage_replay_stale$")):
            native.verify_cached("uv_bake_orm", self.root, record)
        with (self.subTest(boundary="cached_identity"),
              self.assertRaisesRegex(engine.Refusal, "^look_fixture_cage_replay_stale$")):
            default.verify_cached("uv_bake_orm", self.root, record)

        named_low = self.root / "named_low.glb"
        named_low.write_bytes(subject.read_bytes())
        replay = bake.verify_bundle(target, expected_receipt_sha256=result["receipt_sha256"],
                                    read_checked=native._bake_look_scope()[0]["read_checked"])
        self.assertEqual(replay["rc"], 0, replay)
        core.atomic_write_json(self.root / "look-verification.json",
                               {**replay, "look_witness_digest": native._bake_look_scope()[0]["witness_digest"]})
        with (self.subTest(boundary="export_replay"),
              self.assertRaisesRegex(engine.Refusal, "^look_fixture_cage_replay_stale$")):
            native._protected_export_binding(target / "textured.glb", named_low, native._bake_look_scope()[0])

    def test_refuses_malformed_export_previous(self):
        native = self.register(self.low, omit_derivative=True)
        base = {"toolchain_sha256": native._pin("export")}
        for previous in ({}, None, [], {"path": 5, "sha256": "0" * 64},
                         {"path": "/", "sha256": "0" * 64},
                         {"path": "relative", "sha256": "0" * 64},
                         {"path": str(self.root / "stage.json"), "sha256": None}):
            with self.subTest(previous=previous):
                with self.assertRaisesRegex(engine.Refusal, "^look_export_previous_invalid$") as refusal:
                    native.verify_cached("export", self.root, {**base, "previous": previous})
                self.assertEqual(refusal.exception.rc, 2)

    def test_protected_receipt_reopen_refuses_stale_and_above_cap(self):
        native = self.register(self.low, omit_derivative=True)
        scope, _, _ = native._bake_look_scope()
        subject = self.authority / "uv-derivative-glb" / bytes_digest(self.low)[7:]
        root = subject.parent
        bundle = root / "bake"
        bundle.mkdir()
        for name, data in (("source_high.glb", self.high.read_bytes()), ("source_low.glb", self.low)):
            (bundle / name).write_bytes(data)
        receipt_path = bundle / "look_receipt.json"
        receipt_path.write_bytes(b'{}' + b' ' * engine.MAX_JSON)
        digest = core.sha256_file(receipt_path)
        core.atomic_write_json(root / "bake.json", {})
        manifest = {"policy": {"cage_extrusion_um": 5000}, "files": [
            {"path": "look_receipt.json", "sha256": digest},
            {"path": "textured.glb", "sha256": "0" * 64},
        ]}
        manifest_path = bundle / "bundle_receipt.json"
        core.atomic_write_json(manifest_path, manifest)
        replay = {"rc": 0, "receipt_sha256": core.sha256_file(manifest_path)}
        core.atomic_write_json(root / "look-verification.json", replay)

        def authenticate_current_receipt():
            manifest["files"][0]["sha256"] = core.sha256_file(receipt_path)
            core.atomic_write_json(manifest_path, manifest)
            replay["receipt_sha256"] = core.sha256_file(manifest_path)
            core.atomic_write_json(root / "look-verification.json", replay)

        with self.assertRaisesRegex(engine.Refusal, "^json_byte_budget$") as ordinary:
            engine.load(receipt_path, expected_sha256=digest)
        self.assertEqual(ordinary.exception.rc, 2)

        # Exercise the second read independently of the earlier full bundle replay.
        candidate = root / "asset.glb"
        with (patch.object(native, "_verify_bake_look", return_value=replay),
              patch.object(uv_derivative, "verify_export", return_value={}) as verify):
            self.assertEqual(native._protected_export_binding(candidate, subject, scope), replay)
            verify.assert_called_once_with(candidate, bundle, {}, "0" * 64)
            verify.reset_mock()
            with receipt_path.open("r+b") as stream:
                stream.write(b"[]")
            with self.assertRaisesRegex(engine.Refusal, "^bytes_stale$") as stale:
                native._protected_export_binding(candidate, subject, scope)
            self.assertEqual(stale.exception.rc, 4)
            with receipt_path.open("r+b") as stream:
                stream.truncate(stages.bake_support.MAX_BYTES + 1)
            authenticate_current_receipt()
            with self.assertRaisesRegex(stages.bake_support.Refusal, "^file_budget_exceeded$") as above:
                native._protected_export_binding(candidate, subject, scope)
            self.assertEqual(above.exception.rc, 6)
            verify.assert_not_called()
            for raw, reason, error in (
                (b'{"map":0,"map":1}', "^duplicate_json_key$", stages.bake_support.Refusal),
                (br'{"map":"\ud800"}', "^schema_mismatch", core.ContractError),
            ):
                with self.subTest(receipt=raw):
                    receipt_path.write_bytes(raw)
                    authenticate_current_receipt()
                    with self.assertRaisesRegex(error, reason):
                        native._protected_export_binding(candidate, subject, scope)
                    verify.assert_not_called()
