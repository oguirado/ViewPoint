"""Fixture-only replay through factory entry points, with optional retained evidence."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.blender import bake, glb_io, uv
from tools.blender.glb_capture import run_owned
from tools.pipeline import engine, run_asset, stages
from tools.qualification import qualify

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures_sat_replay"
GEOMETRY = ("mesh", "topology", "uv_bake_orm", "export")
SAMPLER = {"magFilter": 9729, "minFilter": 9729, "wrapS": 10497, "wrapT": 10497}


def sha(path):
    return core.sha256_file(path)


def write(path, data):
    core.atomic_write_json(path, data)
    return path


def stage_record(result, name):
    return next(row for row in result["stages"] if row["stage"] == name)


def output_for(row):
    return Path(engine.load(row["receipt"]["path"])["subject"])


class Replay(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("SAT_REPLAY_DIR")
        if not root:
            self.skipTest("SAT_REPLAY_DIR must name an isolated replay evidence directory")
        self.root = Path(root) / self._testMethodName
        self.root.mkdir(parents=True)

    def invoke_pipeline(self, mode, source, reference, out, *flags):
        args = ["--" + mode, str(source), "--reference", str(reference),
                "--provider", "byo" if mode == "glb" else "trellis2",
                "--out", str(out), "--timeout-s", "90", *map(str, flags)]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = run_asset.main(args)
        log = out.parent / (out.name + ".jsonl")
        log.write_text(stdout.getvalue())
        records = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(records[-1]["rc"], rc)
        return records[-1], records[:-1]

    def row(self, name, inputs, outputs, receipt, started):
        return {"transition": name, "inputs": [engine.file_ref(p) for p in inputs],
                "outputs": [engine.file_ref(p) for p in outputs],
                "receipt": engine.file_ref(receipt), "wall_ns": time.monotonic_ns() - started}

    def test_input_and_qualification_boundaries(self):
        from gates.input_d18 import prepare_matte
        from providers.trellis2.qualification.adapter import validated_image
        from test_d18_input_contract import Mattes, png
        from test_f1u3_input_gate import calibration, png as legacy_png, policy as legacy_policy

        fixture = Mattes()
        fixture.setUp()
        started = time.monotonic_ns()
        master = png("RGBA", fixture.size, [
            value for i, alpha in enumerate(fixture.alpha)
            for value in (*fixture.rgb[i * 3:i * 3 + 3], alpha)
        ])
        original = self.root / "original.png"
        original.write_bytes(fixture.original)
        master_path = self.root / "straight-alpha.png"
        master_path.write_bytes(master)
        mask = self.root / "mask.png"
        mask.write_bytes(fixture.reference)
        intent = fixture.store.add("matte-intent", fixture.intent)
        submitted, matte = prepare_matte(fixture.original, master, fixture.reference,
                                        intent_digest=intent, read_checked=fixture.store.read,
                                        matte_source=fixture.matte_source)
        derivative = self.root / "submitted.png"
        derivative.write_bytes(submitted)
        matte_path = write(self.root / "matte.json", matte)
        self.assertEqual(matte["source_rc"], 0, matte)
        self.assertEqual(validated_image(derivative, matte), sha(derivative))
        rows = [self.row("D18 INPUT → Q-T adapter validation", [original, master_path, mask],
                         [derivative], matte_path, started)]

        # The legacy fixture policy is pinned and calibrated only within this test.
        input_policy = legacy_policy()
        (self.root / "calibration.txt").write_bytes(calibration(input_policy))
        policy_path = write(self.root / "input-policy.json", input_policy)
        rgba = self.root / "legacy-rgba.png"
        rgba.write_bytes(legacy_png())
        started = time.monotonic_ns()
        result, inner = self.invoke_pipeline("image", derivative, original, self.root / "rgb-pipeline",
                                             "--input-policy", policy_path)
        input_row = stage_record(result, "input")
        self.assertEqual(inner[-1]["metrics"]["mode"], "RGB", inner)
        self.assertEqual(engine.load(input_row["receipt"]["path"])["reason_codes"], ["input_gate_refused"])
        self.assertEqual(engine.load(stage_record(result, "provider")["receipt"]["path"])["reason_codes"],
                         ["upstream_blocked"])
        self.assertIsNone(result["artifact"])
        rows.append(self.row("D18 RGB → ordinary image pipeline", [derivative, policy_path], [],
                             input_row["receipt"]["path"], started))

        started = time.monotonic_ns()
        legal_shape, _ = self.invoke_pipeline("image", rgba, original, self.root / "rgba-pipeline",
                                              "--input-policy", policy_path)
        self.assertEqual(stage_record(legal_shape, "input")["rc"], 0, legal_shape)
        provider_row = stage_record(legal_shape, "provider")
        self.assertEqual(engine.load(provider_row["receipt"]["path"])["reason_codes"],
                         ["trellis2_service_not_built_or_transport_absent"])
        rows.append(self.row("Legacy RGBA → ordinary provider boundary", [rgba, policy_path], [],
                             provider_row["receipt"]["path"], started))

        cited = os.environ["SAT_TRELLIS_CANDIDATE"]
        candidate = engine.load(cited)
        self.assertEqual(candidate["provider"], "trellis2")
        started = time.monotonic_ns()
        # A tripwire protects the lane from launching either QUAL leg if rights
        # change. The real QUAL manifest preflight and receipt writer still run.
        with patch("tools.qualification.harness.run_offline",
                   side_effect=AssertionError("QUAL attempted to launch")) as launch:
            denied = qualify(candidate, self.root / "qualification", timeout_s=15)
        launch.assert_not_called()
        self.assertEqual(denied["reason_codes"], ["commercial_eu_rights_unproven"], denied)
        self.assertEqual(denied["runs"], [])
        self.assertEqual(denied["rc"], 5)
        rows.append(self.row("Pinned Q-T draft → standalone QUAL", [cited, candidate["rights"]["path"]],
                             [], self.root / "qualification/qualification.json", started))
        write(self.root / "transitions.json", rows)

    def make_geometry(self):
        from test_n18_int_geometry import scope_for_fixture

        blender = shutil.which("blender")
        self.assertIsNotNone(blender)
        run_owned(["taskset", "-c", "0-15", "nice", "-n", "15", blender, "-b", "--factory-startup",
                   "--threads", "2", "--python-exit-code", "2", "--python",
                   str(ROOT / "tools/pipeline/fixture_blender.py"), "--", str(self.root)],
                  timeout_s=120, cwd=self.root)
        self.source, self.reference = self.root / "prop.glb", self.root / "reference.png"
        bake_dir = self.root / "bake"
        receipt = bake.bake_bundle(self.source, self.source, bake_dir, res=64, timeout_s=90)
        self.assertEqual(receipt["rc"], 0, receipt)
        doc, binary = glb_io.decode_glb((bake_dir / "low_uv.glb").read_bytes())
        doc["nodes"][0]["name"] = "body"
        expected_uv = glb_io.encode_glb(doc, binary)
        baked_subject = self.root / "named_low.glb"
        baked_subject.write_bytes(expected_uv)
        policy = engine.load(FIXTURES / "mesh-policy.json")
        self.mesh_policy = write(self.root / "mesh-policy.json", policy)
        backend = stages.NativeStages(source=self.source, reference=self.reference, provider="byo", options={
            "mesh_policy": self.mesh_policy, "timeout_s": 90,
        })
        expected = self.root / "expected"
        expected.mkdir()
        backend.stage_export(expected, baked_subject)
        raw = self.source.read_bytes()
        self.without_scope = write(self.root / "without-scope.json", policy)
        policy["geometry_scope"] = scope_for_fixture(self.root / "authority", raw, {
            "mesh": raw, "topology": raw, "uv_bake_orm": expected_uv,
            "export": (expected / "asset.glb").read_bytes(),
        }, cube=True)
        write(self.mesh_policy, policy)

    def byo(self, out, policy):
        result, _ = self.invoke_pipeline("glb", self.source, self.reference, out,
                                         "--mesh-policy", policy, "--texture-resolution", "64")
        return result

    def verify_export(self, path, uv_subject):
        return uv.check_textured_glb(path, expected_maps={
            name: sha(uv_subject.parent / "bake" / (name + ".png"))
            for name in ("albedo", "normal", "orm")
        }, sampler_policy=SAMPLER, expected_geometry=uv_subject, expected_geometry_sha256=sha(uv_subject))

    def test_geometry_export_and_controls(self):
        self.make_geometry()
        out = self.root / "pipeline"
        started = time.monotonic_ns()
        result = self.byo(out, self.mesh_policy)
        self.assertEqual(result["verdict"], "CANNOT_SEE", result)
        self.assertIsNone(result["artifact"])
        rows = []
        for name in GEOMETRY:
            row = stage_record(result, name)
            at = Path(row["receipt"]["path"])
            engine.authenticate(row["receipt"])
            stage = engine.load(at)
            proof_path = at.parent / stages.GEOMETRY_RECEIPT
            proof = engine.load(proof_path)
            artifact = output_for(row)
            self.assertEqual(row["rc"], 0, stage)
            self.assertEqual(proof["schema"], "af.mesh-regression/v2")
            self.assertEqual(proof["base"]["gate"]["verdict"], "PASS")
            self.assertEqual(proof["subjects"][-1], "sha256:" + sha(artifact))
            rows.append({
                "transition": name, "inputs": stage["inputs"], "outputs": stage["outputs"],
                "receipt": row["receipt"], "geometry_receipt": engine.file_ref(proof_path),
                "wall_ns": stage["wall_ms"] * 1_000_000,
            })
        appearance = stage_record(result, "appearance")
        self.assertEqual(engine.load(appearance["receipt"]["path"])["reason_codes"],
                         ["appearance_request_absent"])
        export = output_for(stage_record(result, "export"))
        uv_subject = output_for(stage_record(result, "uv_bake_orm"))
        fresh_wall_ns = time.monotonic_ns() - started
        started = time.monotonic_ns()
        reopened = self.verify_export(export, uv_subject)
        self.assertEqual(reopened["rc"], 0, reopened)
        rows.append(self.row("independent read-only GLB reopen", [export, uv_subject], [],
                             write(self.root / "reopen.json", reopened), started))
        self.publish_review_copy(export, uv_subject, appearance, rows)

        cached = self.byo(out, self.mesh_policy)
        self.assertTrue(all(stage_record(cached, name)["cache_hit"] for name in GEOMETRY))
        self.assertEqual(sha(export), next(item["sha256"] for item in rows[3]["outputs"]
                                          if item["path"] == str(export)))
        # The independent semantic replay must reject a self-consistent cache-local
        # substitution, even when every cache-local byte reference has been updated.
        at = Path(stage_record(cached, "topology")["receipt"]["path"])
        record = engine.load(at)
        proof_path = at.parent / stages.GEOMETRY_RECEIPT
        original = proof_path.read_bytes()
        original_stage = at.read_bytes()
        marker = at.parent / "entry.json"
        original_marker = marker.read_bytes()
        replacement = engine.load(proof_path)
        replacement["work"] += 1
        try:
            write(proof_path, replacement)
            for evidence in record["evidence"]:
                if evidence["path"] == str(proof_path):
                    evidence.update(engine.file_ref(proof_path))
            write(at, record)
            write(marker, dict(engine.load(marker), receipt=engine.file_ref(at)))
            stale = stage_record(self.byo(out, self.mesh_policy), "topology")
            stale_record = engine.load(stale["receipt"]["path"])
        finally:
            proof_path.write_bytes(original)
            at.write_bytes(original_stage)
            marker.write_bytes(original_marker)
        omitted = stage_record(self.byo(self.root / "missing-scope", self.without_scope), "mesh")
        omitted_record = engine.load(omitted["receipt"]["path"])
        damaged = self.root / "damaged.glb"
        damaged.write_bytes(export.read_bytes()[:-1])
        invalid = self.verify_export(damaged, uv_subject)
        write(self.root / "damaged-reopen.json", invalid)
        write(self.root / "transitions.json", {"rows": rows, "fresh_pipeline_wall_ns": fresh_wall_ns,
             "cached_pipeline_wall_ms": cached["wall_ms"], "controls": {
                 "substituted_upstream": stale, "omitted_scope": omitted,
                 "damaged_export": engine.file_ref(self.root / "damaged-reopen.json"),
             }})
        if os.environ.get("SAT_REPLAY_RED") == "1":
            with self.subTest(control="substituted cached proof looks nominal"):
                self.assertEqual(stale["rc"], 0, stale_record)
            with self.subTest(control="missing scope looks nominal"):
                self.assertEqual(omitted["rc"], 0, omitted_record)
            with self.subTest(control="damaged export looks nominal"):
                self.assertEqual(invalid["rc"], 0, invalid)
        else:
            self.assertEqual((stale["rc"], stale["cache_hit"], stale_record["reason_codes"]),
                             (4, False, ["geometry_certificate_stale"]))
            self.assertEqual((omitted["rc"], omitted_record["reason_codes"]),
                             (3, ["geometry_scope_missing"]))
            self.assertEqual((invalid["rc"], invalid["reason_codes"]), (2, ["glb_invalid"]))

    def publish_review_copy(self, export, uv_subject, appearance, rows):
        from tools.publish import publisher

        started = time.monotonic_ns()
        runtime = self.root / "pub-locks"
        runtime.mkdir(mode=0o700)
        config = {"schema": "af.publish-config/v1", "destination_root": ":local:" + str(self.root / "review"),
                  "source_root": str(self.root), "busy_attempts": 5}
        session = publisher.NamespaceAuthority({"fixture": config["destination_root"]}).session("fixture")
        receipt = Path(appearance["receipt"]["path"])
        descriptor = {"asset_class": "props", "asset_id": "sat-fixture", "version": "v0001",
                      "final_verdict": "CANNOT_SEE", "files": [
                          {"source": str(export), "path": "asset.glb"},
                          {"source": str(receipt), "path": "receipts/appearance.json"},
                      ], "provenance": {
                          "inputs": [{"role": "reference", "sha256": sha(self.reference)}],
                          "model": {"id": "byo-fixture", "revision": "sat-fixture"},
                          "licence": None, "commercial_use": None, "absence_reasons": ["appearance_request_absent"],
                          "gates": [{"name": "appearance", "verdict": "CANNOT_SEE", "numbers": {},
                                     "receipt": "receipts/appearance.json", "verdict_key": "verdict"}],
                      }}
        plan = publisher.build_plan({"schema": "af.publish-source/v1", "assets": [descriptor]}, config)
        plan_path = write(self.root / "pub-plan.json", plan)
        approval = {"schema": "af.publish-approval/v1", "approved": True,
                    "plan_sha256": core.sha256_json(plan), "destination_root": plan["destination_root"],
                    "allow_folder_creation": True, "allow_metadata_updates": True}
        with patch.object(publisher, "lock_user_root", return_value=runtime):
            published = publisher.publish(plan, config=config, approval=approval, session=session)
        published_path = write(self.root / "pub-receipt.json", published)
        self.assertEqual(published["rc"], 0, published)
        self.assertEqual(published["commands"], [])
        review_path = plan["assets"][0]["destination"].removeprefix(plan["destination_root"])
        self.assertTrue(review_path.startswith("_review/"), published)
        review_copy = self.root / "review" / review_path / "asset.glb"
        self.assertEqual(sha(review_copy), sha(export))
        rows.append(self.row("explicit local PUB review copy", [export, plan_path, receipt],
                             [review_copy], published_path, started))

        started = time.monotonic_ns()
        store = self.root / "restore"
        store.mkdir()
        import_plan = publisher.plan_import(config=config, store=store, session=session)
        import_path = write(self.root / "pub-import-plan.json", import_plan)
        restored = publisher.restore(import_plan, config=config, session=session)
        restored_path = write(self.root / "pub-import-receipt.json", restored)
        self.assertEqual(restored["rc"], 0, restored)
        (asset,) = list(store.rglob("asset.glb"))
        self.assertEqual(sha(asset), sha(export))
        self.assertEqual(self.verify_export(asset, uv_subject)["rc"], 0)
        rows.append(self.row("PUB local import and rehash", [import_path, export], [asset],
                             restored_path, started))

    def test_protected_look_boundary(self):
        import test_n18_lookbake_materials as look_fixture

        started = time.monotonic_ns()
        high = look_fixture.sheet((0, -39322, 52429))
        low, store, pin, _, _ = look_fixture.fixture(high=high, relief=True, measured_orm=False)
        high_path, low_path = self.root / "high.glb", self.root / "low.glb"
        high_path.write_bytes(high)
        low_path.write_bytes(low)
        witness_dir = self.root / "witnesses"
        witness_dir.mkdir()
        for (role, digest), data in store.records.items():
            (witness_dir / (role + "-" + digest.removeprefix("sha256:"))).write_bytes(data)
        witness_files = sorted(witness_dir.iterdir())
        output = self.root / "bundle"
        receipt = bake.bake_bundle(high_path, low_path, output, res=look_fixture.SIZE, margin_px=2,
                                   blender_threads=1, look_scope={"witness_digest": pin,
                                                                 "read_checked": store.read})
        write(self.root / "bake-result.json", receipt)
        self.assertEqual(receipt["rc"], 0, receipt)
        expected = receipt["receipt_sha256"]
        checked = bake.verify_bundle(output, expected_receipt_sha256=expected, read_checked=store.read)
        self.assertEqual((checked["rc"], checked["scope"]), (0, "protected-look-replay"), checked)
        declined = bake.verify_bundle(output, expected_receipt_sha256=expected, production=True)
        self.assertEqual(declined["reason_codes"], ["factory_manifest_schema_unassigned"], declined)
        write(self.root / "look-production-refusal.json", declined)
        rows = [self.row("Protected UV/bake fixture", [high_path, low_path, *witness_files],
                         [output / "textured.glb"], output / "look_receipt.json", started)]
        write(self.root / "look-reopen.json", checked)
        write(self.root / "transitions.json", rows)

    def test_face_and_voice_boundaries(self):
        from test_d18_voice_track import build, check, labels, provider_fixture, timing_fixture
        from test_n18_face_blender import run_blender

        started = time.monotonic_ns()
        face_source = Path(os.environ["SAT_FACE_SOURCE"])
        output = self.root / "face"
        run_blender("build", face_source, output)
        run_blender("verify", face_source, output, output / "head.blend")
        face_path = output / "receipt.json"
        face = engine.load(face_path)
        self.assertEqual(face["fixture_verdict"], "PASS", face)
        rows = [self.row("FACE build and reopen", sorted(p for p in face_source.rglob("*") if p.is_file()),
                         [output / "head.blend"], face_path, started)]

        started = time.monotonic_ns()
        audio, timing = self.root / "voice.wav", self.root / "alignment.json"
        audio.write_bytes(timing_fixture())
        timing.write_bytes(provider_fixture())
        track = build(audio.read_bytes(), alignment=timing.read_bytes())
        witness = labels()
        for field in ("mouth_onsets", "silences"):
            witness[field] = [list(row) for row in witness[field]]
        witness_path = write(self.root / "voice-witness.json", witness)
        verdict = check(track, audio.read_bytes(), witness)
        self.assertEqual((verdict["fixture_verdict"], verdict["production_verdict"]), ("PASS", "CANNOT_SEE"))
        track_path = write(self.root / "track.json", track)
        voice_path = write(self.root / "voice-timing.json", verdict)
        rows.append(self.row("VOICE alignment and timing witness", [audio, timing, witness_path], [track_path], voice_path,
                             started))
        write(self.root / "transitions.json", rows)

    def test_skin_act_boundaries(self):
        from asset_factory import math_acceptance
        from providers.unirig import gate
        from test_n18_act_replay import DenseReplayTests

        source = Path(os.environ["ACT_EVIDENCE_DIR"])
        started = time.monotonic_ns()
        rig = source / "skin.glb"
        anatomy = source / "anatomy.json"
        anatomy_bytes = anatomy.read_bytes()
        pin = math_acceptance.canonical_digest(json.loads(anatomy_bytes))
        policy_path = ROOT / "providers/unirig/gate-policy.json"
        skin = gate.inspect_skin_bytes(rig.read_bytes(), policy=engine.load(policy_path), fixture_digest=pin,
                                      read_checked=lambda role, expected: anatomy_bytes
                                      if (role, expected) == ("skin-fixture", pin) else None)
        self.assertEqual(skin["rc"], 0, skin)
        skin_path = write(self.root / "skin-receipt.json", skin)
        rows = [self.row("SKIN decoded rig and protected poses", [rig, anatomy, policy_path], [rig],
                         skin_path, started)]

        started = time.monotonic_ns()
        DenseReplayTests.setUpClass()
        fixture = DenseReplayTests()
        # ACT authenticates the authored action's originally registered SKIN
        # bytes. Keep the fresh replay and the historical input explicit.
        skin_source = source / "skin-receipt.json"
        receipt, trace = fixture.evaluate(fixture.data)
        self.assertEqual(receipt["rc"], 0, receipt)
        receipt_path = write(self.root / "motion-receipt.json", receipt)
        trace_path = write(self.root / "motion-trace.json", trace)
        rows.append(self.row("ACT sampled playback", [rig, anatomy, skin_source, source / "walk-gesture.glb",
                             source / "rp1/registration.json", source / "rp1/support-plane.json"],
                             [trace_path], receipt_path, started))
        write(self.root / "transitions.json", rows)

    def test_physics_boundary(self):
        from test_n18_phys_replay import PhysFixture

        if not os.environ.get("SAT_ENABLE_PHYS"):
            self.skipTest("PHYS replay needs the accepted MuJoCo interpreter")
        started = time.monotonic_ns()
        PhysFixture.setUpClass()
        fixture = PhysFixture()
        fixture.setUp()
        receipt, evidence = fixture.check()
        self.assertEqual(receipt["rc"], 0, receipt)
        self.assertEqual((receipt["threshold_status"], receipt["production_verdict"]),
                         ("TARGET_UNCALIBRATED", "CANNOT_SEE"))
        fixture.save_evidence(self.root, receipt, evidence)
        rows = [self.row("PHYS collision, mass and CPU replay",
                         [self.root / name for name in ("render.glb", "registration.json", "support.json",
                                                        "collider.json")],
                         sorted(self.root.glob("trace-*.json")), self.root / "receipt.json", started)]
        write(self.root / "transitions.json", rows)

    def test_registry_assembly_boundary(self):
        from tools.blender.assembly import pilot
        from tools.registry import Registry

        started = time.monotonic_ns()
        root = self.root / "registry"
        root.mkdir()
        registry = Registry(root, namespace="parts-pilot")
        template = Path(os.environ["SAT_VIEW_TEMPLATE"])
        blender = Path(shutil.which("blender"))
        raw, approved, _ = pilot.author_parts(registry, root=self.root / "author", blender=blender,
                                              view_template=template, expected_snapshot=registry.initialize())
        attempt = self.root / "assembly"
        pilot.assemble(registry, raw, expected_recipe_digest=pilot.pin(raw), approved_parts=approved,
                       expected_snapshot=registry.current_snapshot(), root=attempt,
                       blender=blender, view_template=template)
        registration = engine.load(attempt / "registration.json")
        reader = Registry(root, namespace="parts-pilot")
        locator, snapshot = registration["locator"], registration["snapshot"]
        found = reader.resolve(locator, snapshot=snapshot, **pilot.READ_LIMITS)
        self.assertEqual(set(found["lineage_records"]), set(approved.values()))
        self.assertEqual(found["record"]["gates"], [])
        scene = attempt / "scene.glb"
        self.assertEqual(reader.read_blob(locator, pilot.pin(scene.read_bytes()), snapshot=snapshot),
                         scene.read_bytes())
        rows = [self.row("af.assemble → REG fresh-reader resolution",
                         [attempt / "recipe.json", template, *sorted((self.root / "author").glob("*.glb"))],
                         [scene, attempt / "scene.blend", attempt / "views.json"],
                         attempt / "registration.json", started)]
        write(self.root / "transitions.json", rows)
