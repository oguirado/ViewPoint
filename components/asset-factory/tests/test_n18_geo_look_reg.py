"""Native GEO and protected LOOK regressions with pinned offline fixture inputs."""
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.blender import bake, glb_io, look_bake, topology
from tools.blender.topology_glb import inspect_glb
from tools.input.evidence import bytes_digest
from tools.pipeline import engine, stages, topology_bridge, uv_derivative
from test_e2e_blender import options
from test_n18_int_geometry import fixture_scope
import test_d18_geo_certificate as geometry_fixture
import test_n18_lookbake_materials as look_fixture


FIXTURES = Path(__file__).with_name("fixtures_geo_look_reg")
PINS = json.loads((FIXTURES / "pins.json").read_bytes())


def changed_policy(opts, scopes):
    policy = engine.load(opts["mesh_policy"])
    policy["geometry_scope"] = scopes
    core.atomic_write_json(opts["mesh_policy"], policy)


def write_look_store(root, store):
    for (role, digest), data in store.records.items():
        path = root / role / digest[7:]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)


def receipt(row):
    return engine.load(row["receipt"]["path"], expected_sha256=row["receipt"]["sha256"])


def stage_rows(result):
    return {row["stage"]: row for row in result["stages"]}


class GeometryLookControls(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.opts = options(self.root)
        self.source = self.root / "source.glb"
        self.reference = self.root / "unused-reference.png"
        self.reference.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.authority = self.root / "authority"
        self.authority.mkdir()

    def backend(self):
        return stages.NativeStages(source=self.source, reference=self.reference,
                                   provider="byo", options=self.opts)

    def test_missing_geometry_pair_refuses_native_engine(self):
        fixture = geometry_fixture.Gates()
        fixture.setUp()
        self.source.write_bytes(fixture.raw)
        changed_policy(self.opts, {"authority": str(self.authority)})

        backend = self.backend()
        with patch.object(backend, "stage_mesh", wraps=backend.stage_mesh) as mesh:
            result = engine.Pipeline(self.root / "pipeline", backend).run()
        rows = stage_rows(result)
        self.assertEqual(rows["provider"]["rc"], 0)
        self.assertEqual(receipt(rows["provider"])["outputs"][0]["sha256"], core.sha256_file(self.source))
        self.assertEqual((rows["mesh"]["rc"], receipt(rows["mesh"])["reason_codes"]),
                         (3, ["geometry_scope_missing"]))
        self.assertIsNone(result["artifact"])
        mesh.assert_not_called()

    def test_registered_low_cannot_be_replaced_by_high(self):
        fixture = geometry_fixture.Gates()
        fixture.setUp()
        high = fixture.raw
        self.source.write_bytes(high)
        document, binary = glb_io.decode_glb(high)
        document["nodes"][0]["name"] = "different-fixture-node"
        low = glb_io.encode_glb(document, binary)
        candidate = self.root / "low.glb"
        candidate.write_bytes(low)
        scope = fixture_scope(fixture, self.authority, low)
        changed_policy(self.opts, {"authority": str(self.authority), "uv_bake_orm": scope})
        backend = self.backend()
        engine.Pipeline(self.root / "pipeline", backend)
        backend.bind_raw(self.source)
        proof = backend._geometry_proof("uv_bake_orm", candidate)
        self.assertEqual(proof["subjects"], [bytes_digest(high), bytes_digest(low)])
        with self.assertRaises(engine.Refusal) as caught:
            backend._geometry_proof("uv_bake_orm", self.source)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (4, "geometry_registration_drift"))

    def test_changed_look_witness_refuses_before_bake(self):
        high = look_fixture.sheet((0, -39322, 52429))
        low, store, witness, _, _ = look_fixture.fixture(high=high, relief=True, measured_orm=False)
        self.source.write_bytes(high)
        candidate = self.root / "low.glb"
        candidate.write_bytes(low)
        write_look_store(self.authority, store)
        witness_path = self.authority / "look-bake-witness" / witness[7:]
        changed_policy(self.opts, {"authority": str(self.authority), "look_scope": {"witness_digest": witness}})
        _, pins, _ = self.backend().identity("uv_bake_orm", candidate)
        self.assertEqual(pins["bake_sources"], {"high_sha256": core.sha256_file(self.source),
                                               "low_sha256": core.sha256_file(candidate)})
        witness_path.write_bytes(witness_path.read_bytes() + b" ")
        with (patch.object(bake, "bake_bundle", wraps=bake.bake_bundle) as launch,
              self.assertRaises(engine.Refusal) as caught):
            self.backend().stage_uv_bake_orm(self.root, candidate)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (4, "look_evidence_stale"))
        launch.assert_not_called()


@unittest.skipUnless(os.environ.get("AF_GEO_LOOK_REG_CAMPAIGN"),
                     "offline campaign fixture root not configured")
class ArchivedGeometryLook(unittest.TestCase):
    def setUp(self):
        self.campaign = Path(os.environ["AF_GEO_LOOK_REG_CAMPAIGN"]).resolve(strict=True)
        self.bundle = self.campaign / PINS["look"]["root"] / "stage" / "bake"
        self.look_authority = self.bundle.parents[1] / "authority"

    def checked_file(self, root, ref):
        name, digest = ref
        path = root / name
        self.assertEqual(core.sha256_file(path), digest, path)
        return path

    def look_reader(self, role, pin):
        return self.checked_file(self.look_authority, (f"{role}/{pin[7:]}", pin[7:])).read_bytes()

    def test_reopens_accepted_look_bundle(self):
        pins = PINS["look"]
        high = self.checked_file(self.bundle, ("source_high.glb", pins["high"]))
        low = self.checked_file(self.bundle, ("source_low.glb", pins["low"]))
        self.checked_file(self.bundle, ("textured.glb", pins["textured"]))
        bundle_receipt = self.checked_file(self.bundle, ("bundle_receipt.json", pins["bundle_receipt"]))
        witness = json.loads(self.look_reader("look-bake-witness", pins["witness"]))
        self.assertEqual(witness["atlas_digest"], pins["atlas"])
        historical = bake.verify_bundle(self.bundle, expected_receipt_sha256=pins["bundle_receipt"],
                                         read_checked=self.look_reader)
        tools_match = engine.load(bundle_receipt)["tool_files"] == bake._tool_files(protected=True)
        if tools_match:
            self.assertEqual(historical["rc"], 0, historical)
        else:
            self.assertEqual((historical["rc"], historical["reason_codes"]), (4, ["toolchain_drift"]))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            opts = options(root)
            changed_policy(opts, {"authority": str(self.look_authority),
                                  "look_scope": {"witness_digest": pins["witness"]}})
            native = stages.NativeStages(source=high, reference=self.bundle.parents[1] / "reference.png",
                                        provider="byo", options=opts)
            started = time.monotonic()
            result = native.stage_uv_bake_orm(root, low)
            verification = engine.load(root / "look-verification.json")
            self.assertEqual(result["rc"], 0, result)
            self.assertEqual((verification["rc"], verification["scope"], verification["production_evidence"]),
                             (0, "protected-look-replay", False), verification)
            textured = root / "bake/textured.glb"
            maps = look_bake.read_look_maps(textured.read_bytes(), low.read_bytes())
            rgba = look_bake.decode_look_png(maps["normal"], look_fixture.SIZE)
            self.assertEqual(rgba[4 * look_fixture.RELIEF_TEXEL + 1], 204)
            print("LOOK_REPLAY", json.dumps({
                "historical": historical,
                "fresh_bundle_receipt_sha256": core.sha256_file(root / "bake/bundle_receipt.json"),
                "fresh_textured_sha256": core.sha256_file(textured),
                "reopened": verification,
                "wall_s": time.monotonic() - started,
            }, sort_keys=True), flush=True)

    def test_legacy_export_reencoding_loses_protected_sampler(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.bundle / "source_high.glb"
            low = self.bundle / "source_low.glb"
            target = root / "uv_bake_orm"
            target.mkdir()
            shutil.copyfile(low, target / "named_low.glb")
            shutil.copytree(self.bundle, target / "bake")
            out = root / "export"
            out.mkdir()
            opts = options(root)
            policy = engine.load(opts["mesh_policy"])
            policy.update(allow_open=True, component_allowlist={"sheet": 1})
            core.atomic_write_json(opts["mesh_policy"], policy)
            native = stages.NativeStages(source=source, reference=self.bundle.parents[1] / "reference.png",
                                        provider="byo", options=opts)
            try:
                export = native.stage_export(out, target / "named_low.glb")
            except engine.Refusal as exc:
                print("LEGACY_EXPORT", exc.rc, exc.reason, flush=True)
            else:
                print("LEGACY_EXPORT", export["rc"], flush=True)
            # Inspect even a rejected candidate. The exporter writes it before
            # applying the geometry and texture binding checks.
            candidate = (out / "asset.glb").read_bytes()
            document, _ = glb_io.decode_glb(candidate)
            print("LEGACY_EXPORT_WITNESS", json.dumps({
                "digest": bytes_digest(candidate), "samplers": document["samplers"],
            }, sort_keys=True), flush=True)
            with self.assertRaises(look_bake.Refusal) as drift:
                look_bake.read_look_maps(candidate, low.read_bytes())
            self.assertEqual((drift.exception.rc, drift.exception.reason),
                             (5, "look_material_or_sampler_drift"))

    def test_generated_archive_without_pair_refuses(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            high = self.checked_file(self.campaign, PINS["generated"]["high"])
            low = self.checked_file(self.campaign, PINS["generated"]["low"])
            self.assertNotEqual(core.sha256_file(high), core.sha256_file(low))
            opts = options(root)
            reference = root / "unused-reference.png"
            reference.write_bytes(b"\x89PNG\r\n\x1a\n")
            native = stages.NativeStages(source=high, reference=reference, provider="byo", options=opts)
            result = engine.Pipeline(root / "pipeline", native).run()
            rows = stage_rows(result)
            self.assertEqual(receipt(rows["provider"])["outputs"][0]["sha256"], core.sha256_file(high))
            self.assertEqual((rows["mesh"]["rc"], receipt(rows["mesh"])["reason_codes"]),
                             (3, ["geometry_scope_missing"]))
            self.assertIsNone(result["artifact"])

    def test_native_quadriflow_through_protected_look_and_export(self):
        # The archived full-scale replay is opt-in. A configured root with
        # missing or changed evidence is a failure, including missing scopes.
        prepared = Path(os.environ["AF_GEO_LOOK_REG_PREPARED"]).resolve(strict=True)
        run_name = os.environ.get("AF_GEO_LOOK_REG_RUN", "engine-observed")
        self.assertEqual(Path(run_name).name, run_name)
        self.assertNotIn(run_name, {"", ".", ".."})
        evidence = Path(os.environ.get("AF_GEO_LOOK_REG_EVIDENCE", prepared)) / run_name
        evidence.mkdir()
        pins = PINS["quadriflow"]
        high = self.checked_file(prepared, pins["high"])
        low = self.checked_file(prepared, pins["low"])
        cage = self.checked_file(prepared, pins["cage"])
        authority = evidence / "authority"
        shutil.copytree(prepared / "sphere-authority", authority)
        shutil.copytree(self.look_authority, authority, dirs_exist_ok=True)
        scopes = engine.load(prepared / "sphere-scopes.json")
        self.assertEqual(scopes["topology"], pins["geometry_scope"])
        registrations = [
            engine.load(authority / "geo-registration" / pin[7:], expected_sha256=pin[7:])
            for pin in scopes["topology"]["registration_digests"]
        ]
        self.assertEqual([reg["subject"] for reg in registrations],
                         ["sha256:" + pins["high"][1], "sha256:" + pins["low"][1]])
        scopes.update(authority=str(authority), uv_bake_orm=scopes["topology"],
                      look_scope={"witness_digest": PINS["look"]["witness"]})
        mesh_policy = engine.load(prepared / "wire-c-replay" / "policy.json")
        opts = options(evidence)
        opts.update(method="quadriflow", target_faces=pins["target_faces"], timeout_s=120)
        fixture = json.loads((FIXTURES / "look-precision.json").read_bytes())
        bundle_root = self.campaign / fixture["root"]
        for path, digest in fixture["authority_descriptors"]:
            self.checked_file(bundle_root, (path, digest))
        shutil.copytree(bundle_root / "geometry-authority", authority, dirs_exist_ok=True)
        for path, digest in fixture["look_authority_files"]:
            original = self.checked_file(bundle_root / "look-authority", (path, digest))
            target = authority / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
        opts.update(texture_resolution=fixture["resolution"], timeout_s=fixture["timeout_s"])
        mesh_policy["geometry_scope"] = scopes
        core.atomic_write_json(opts["mesh_policy"], mesh_policy)
        other_pair = stages.NativeStages(source=high, reference=prepared / "wire-c-replay/ref.png",
                                        provider="byo", options=opts)
        with (patch.object(uv_derivative, "prepare") as prepare,
              patch.object(bake, "bake_bundle") as launch,
              self.assertRaises(engine.Refusal) as caught):
            other_pair.stage_uv_bake_orm(evidence, low)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (4, "look_subject_drift"))
        prepare.assert_not_called()
        launch.assert_not_called()
        scopes.update(fixture["geometry_scopes"], look_scope=fixture["look_scope"])
        solver = {"quadriflow_target_quads": pins["solver_quads"], "production_evidence": False,
                  "quad_cage_digest": "sha256:" + pins["cage"][1]}
        scopes["quadriflow_solver"] = solver
        mesh_policy["geometry_scope"] = scopes
        core.atomic_write_json(opts["mesh_policy"], mesh_policy)
        native = stages.NativeStages(source=high, reference=prepared / "wire-c-replay/ref.png",
                                    provider="byo", options=opts)
        run = engine.Pipeline(evidence / "pipeline", native)
        sources = [Path(module.__file__) for module in (engine, stages, topology_bridge, bake)]
        source_digests = {str(path): core.sha256_file(path) for path in sources}
        core.atomic_write_json(evidence / "source-digests.json", source_digests)
        result = run.run()
        core.atomic_write_json(evidence / "run-observed.json", result)
        self.assertEqual(source_digests, {str(path): core.sha256_file(path) for path in sources})
        rows = stage_rows(result)
        for stage in ("input", "provider", "mesh", "topology"):
            self.assertEqual(rows[stage]["rc"], 0, receipt(rows[stage]))
            self.assertFalse(rows[stage]["cache_hit"])
        topo_record = receipt(rows["topology"])
        subject = Path(topo_record["subject"])
        emitted_cage = subject.with_suffix(".cage.json")
        topo_dir = subject.parent.parent
        proof = engine.load(topo_dir / stages.GEOMETRY_RECEIPT)
        topo = engine.load(topo_dir / "topology.json")
        operation = engine.load(subject.with_name("operation.json"))
        self.assertEqual(topo_record["parameters"]["quadriflow_solver"], solver)
        self.assertEqual(topo["quadriflow_solver"], solver)
        self.assertEqual(operation["target_faces"], solver["quadriflow_target_quads"])
        self.assertLessEqual(topo["checks"][0]["achieved_faces"], pins["target_faces"])
        repair = Path(receipt(rows["mesh"])["subject"])
        self.assertEqual(topo["raw_sha256"], core.sha256_file(repair))
        self.assertEqual(proof["subjects"], [bytes_digest(high.read_bytes()), bytes_digest(low.read_bytes())])
        self.assertEqual(inspect_glb(high)["faces"], pins["high_faces"])
        self.assertEqual(inspect_glb(subject)["faces"], pins["low_faces"])
        self.assertEqual(core.sha256_file(subject), core.sha256_file(low))
        self.assertEqual(core.sha256_file(emitted_cage), core.sha256_file(cage))
        self.assertIn(engine.file_ref(emitted_cage), topo_record["evidence"])
        self.assertEqual(proof["levels"][1]["quad_cage"]["quads"], pins["quad_count"])
        self.assertEqual(proof["levels"][1]["quad_cage"]["cage_digest"], bytes_digest(cage.read_bytes()))
        with self.assertRaises(engine.Refusal) as caught:
            native._geometry_proof("uv_bake_orm", high)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (4, "geometry_registration_drift"))
        self.assertEqual(result["verdict"], "CANNOT_SEE")
        self.assertIsNone(result["artifact"])
        self.assertEqual([rows[name]["rc"] for name in ("uv_bake_orm", "export")], [0, 0],
                         {name: receipt(rows[name])["reason_codes"] for name in ("uv_bake_orm", "export")})
        uv_stage = receipt(rows["uv_bake_orm"])
        export_stage = receipt(rows["export"])
        derivative = Path(uv_stage["subject"])
        candidate = Path(export_stage["subject"])
        self.assertEqual(core.sha256_file(derivative), fixture["derivative_sha256"])
        self.assertEqual(core.sha256_file(candidate), fixture["export_sha256"])
        self.assertEqual(uv_derivative.same_geometry(subject.read_bytes(), derivative.read_bytes(),
                                                      max_faces=pins["target_faces"]), pins["low_faces"])
        bundle = derivative.parent / "bake"
        self.assertEqual(candidate.read_bytes(), (bundle / "textured.glb").read_bytes())
        export_binding = engine.load(candidate.parent / "protected-export.json")
        uv_binding = engine.load(derivative.parent / "look-verification.json")
        maps = {role: bytes_digest(raw) for role, raw in
                look_bake.read_look_maps(candidate.read_bytes(), derivative.read_bytes()).items()}
        self.assertEqual(maps, export_binding["map_digests"])
        self.assertEqual(maps, fixture["export_map_digests"])
        self.assertFalse(export_binding["production_evidence"])
        self.assertFalse(uv_binding["production_evidence"])
        self.assertIn("factory_manifest_schema_unassigned", result["reason_codes"])
        for name, row, expected in (("uv_bake_orm", uv_stage, derivative), ("export", export_stage, candidate)):
            proof = engine.load(expected.parent / stages.GEOMETRY_RECEIPT)
            self.assertEqual(proof["subjects"], [bytes_digest(high.read_bytes()), bytes_digest(expected.read_bytes())])
            self.assertEqual(proof["base"]["gate"]["receipt_digest"], scopes[name]["stage_digest"])
            self.assertEqual(row["rc"], 0)
        print("MATCHED_LOOK_EXPORT", json.dumps({
            "stage_wall_ms": {name: receipt(row)["wall_ms"] for name, row in rows.items()},
            "run_wall_ms": result["wall_ms"], "candidate": str(candidate), "map_digests": maps,
        }, sort_keys=True), flush=True)
