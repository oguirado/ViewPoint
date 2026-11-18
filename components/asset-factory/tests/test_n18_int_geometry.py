"""Real native BYO promotion through mesh and topology, with fixture-only BASE evidence."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from asset_factory import core
from tools.blender.glb_io import decode_glb, encode_glb
from tools.input.evidence import bytes_digest
from tools.mesh import certificate, raw_admission
from tools.pipeline import engine, stages
import test_d18_base_evidence as base_fixture
import test_d18_geo_certificate as geo_fixture
from test_e2e_blender import options


def fixture_scope(geometry, root, candidate=None):
    candidate = geometry.raw if candidate is None else candidate
    domain = geometry.domain_pin()
    registration = geometry.registration(candidate, domain, geometry.patches)
    pins = dict(domain_digest=domain, registration_digests=[geometry.policy["raw_registration_digest"],
                                                            registration])
    native = certificate.certify([geometry.raw, candidate], read_checked=geometry.store.read, **pins)
    if native["measurement_rc"] != 0:
        raise AssertionError(native["reason_codes"])
    base = base_fixture.EvidenceTests()
    base.setUp()
    base.store = geometry.store
    base.profile.update(native["base_scope"], criteria=[])
    base.receipt.update(native["base_scope"], observations=[], work_counts={"geometry": native["work"]},
                        subject_digest=bytes_digest(candidate))
    del base.receipt["role"], base.receipt["operation"]
    control = {k: base.profile[k] for k in ("policy_digest", "domain_digest", "gate_id",
                                            "evaluator_digest", "toolchain_digest")}
    control.update(expected_rc=5, observed_rc=5, witness_digest=bytes_digest(b"test control"))
    controls = [base.store_object("negative-control", control)]
    base.profile["negative_control_receipt_refs"] = base.receipt["negative_control_receipt_refs"] = controls
    for key, check in native["checks"].items():
        rule = {k: v for k, v in check.items() if k not in ("lower", "upper")}
        rule.update(id=key, method_digest=base.profile["evaluator_digest"], uncertainty_kind="bounded",
                    min_confidence_ppm=None, target_reason=None, proof_receipt_digest=None,
                    calibration_receipt_digest=None)
        base.qualify(rule)
        base.profile["criteria"].append(rule)
        base.receipt["observations"].append(dict(id=key, unit=rule["unit"], method_digest=rule["method_digest"],
            lower=check["lower"], upper=check["upper"], confidence_ppm=None))
    pins["profile_digest"] = base.store_object("profile", base.profile)
    base.receipt["profile_digest"] = pins["profile_digest"]
    for observation in base.receipt["observations"]:
        witness = dict(observation, subject_digest=bytes_digest(candidate),
                       profile_digest=pins["profile_digest"], gate_id=base.profile["gate_id"],
                       domain_digest=domain, witness_digest=native["native_digest"])
        observation["certificate_ref"] = base.store_object("certificate", witness)
    pins["stage_digest"] = base.store_object("stage", base.receipt)
    for (role, pin), data in geometry.store.items():
        directory = root / role
        directory.mkdir(parents=True, exist_ok=True)
        (directory / pin[7:]).write_bytes(data)
    return pins


def scope_for_fixture(root, raw, candidates, *, cube=False):
    class _FixtureGeometry(geo_fixture.Gates):
        """Register the split position indices of the known single-shell test solids."""

        def registration(self, blob, domain, patches):
            doc, binary = decode_glb(blob)
            prim = doc["meshes"][0]["primitives"][0]
            positions = raw_admission._accessor(doc, binary, prim["attributes"]["POSITION"], "fff")
            representatives = {}
            welds = [representatives.setdefault(point, index) for index, point in enumerate(positions)]
            if cube:
                # Protect fixed planes of the authored cube across Blender's UV reindexing.
                _, triangles = next(raw_admission._primitives(doc, binary, 12))
                triangles = list(triangles)
                patches = {}
                for name, axis, sign in (("face", 1, -1), ("left_hand", 0, -1), ("right_hand", 0, 1)):
                    faces = [i for i, tri in enumerate(triangles)
                             if len({v[axis] for v in tri}) == 1 and tri[0][axis] * sign > 0]
                    if len(faces) != 2:
                        raise AssertionError((name, faces))
                    patches[name] = ["0:0", faces]
            pin = super().registration(blob, domain, patches)
            record = json.loads(self.store.read("geo-registration", pin))
            record["welded_index_maps"] = {"0:0": welds}
            return self.store.add("geo-registration", record)

    geometry = _FixtureGeometry()
    geometry.setUp()
    geometry.raw = raw
    geometry.policy["source"] = bytes_digest(raw)
    if cube:
        geometry.intent.update(forbidden_boxes_um=[[[-2200000, -100, -100], [-2100000, 100, 100]]],
                               max_glb_bytes=1000000)
        geometry.policy.update(intent=geometry.store.add("raw-intent", geometry.intent), face_budgets=[12])
    scopes = {"authority": str(root)}
    by_candidate = {}
    for stage, candidate in candidates.items():
        if candidate not in by_candidate:
            by_candidate[candidate] = fixture_scope(geometry, root, candidate)
        scopes[stage] = by_candidate[candidate]
    return scopes


class Promotion(unittest.TestCase):
    def test_qualified_then_cache_then_substituted_receipt(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            opts = options(root)
            geometry = geo_fixture.Gates()
            geometry.setUp()
            document, binary = decode_glb(geometry.raw)
            document["nodes"][0]["name"] = "body"
            geometry.raw = encode_glb(document, binary)
            geometry.policy["source"] = bytes_digest(geometry.raw)
            source = root / "raw.glb"
            source.write_bytes(geometry.raw)
            reference = root / "reference.png"
            reference.write_bytes(b"\x89PNG\r\n\x1a\n")
            authority = root / "authority"
            scope = fixture_scope(geometry, authority)
            policy = engine.load(opts["mesh_policy"])
            policy["geometry_scope"] = dict(authority=str(authority), mesh=scope, topology=scope)
            core.atomic_write_json(opts["mesh_policy"], policy)

            def run(backend_type=stages.NativeStages):
                backend = backend_type(source=source, reference=reference, provider="byo", options=opts)
                result = engine.Pipeline(root / "output", backend).run()
                return {row["stage"]: row for row in result["stages"]}

            fresh = run()
            for name in ("mesh", "topology"):
                row = fresh[name]
                record = engine.load(row["receipt"]["path"])
                self.assertEqual(record["rc"], 0, record["reason_codes"])
                self.assertFalse(row["cache_hit"])
                self.assertIn(str(Path(row["receipt"]["path"]).parent / stages.GEOMETRY_RECEIPT),
                              [ref["path"] for ref in record["evidence"]])
            cached = run()
            self.assertTrue(all(cached[name]["cache_hit"] for name in ("mesh", "topology")))
            self.assertEqual(cached["topology"]["receipt"], fresh["topology"]["receipt"])

            class ChangedToolchain(stages.NativeStages):
                drift = False

                def verify_cached(self, name, directory, record):
                    super().verify_cached(name, directory, record)
                    if name == "provider":
                        self.drift = True

                def _pin(self, name):
                    if self.drift and name == "input":
                        return "a" * 64
                    return super()._pin(name)

            stale = run(ChangedToolchain)["mesh"]
            self.assertFalse(stale["cache_hit"])
            self.assertEqual(engine.load(stale["receipt"]["path"])["reason_codes"],
                             ["upstream_toolchain_stale"])
            # Replace the native certificate AND update both cache-local hashes.
            # The approved stage bytes and fresh geometry still identify the real proof.
            receipt = Path(cached["topology"]["receipt"]["path"])
            stage = engine.load(receipt)
            proof_file = receipt.parent / stages.GEOMETRY_RECEIPT
            original = proof_file.read_bytes()
            proof = engine.load(proof_file)
            proof["work"] += 1
            core.atomic_write_json(proof_file, proof)
            for ref in stage["evidence"]:
                if ref["path"] == str(proof_file):
                    ref.update(engine.file_ref(proof_file))
            core.atomic_write_json(receipt, stage)
            marker = receipt.parent / "entry.json"
            core.atomic_write_json(marker, dict(engine.load(marker), receipt=engine.file_ref(receipt)))
            tampered = run()["topology"]
            self.assertEqual(tampered["rc"], 4, engine.load(tampered["receipt"]["path"]))
            self.assertFalse(tampered["cache_hit"])
            # A cached mesh must authenticate the independently stored BASE receipt too.
            stage_path = authority / "stage" / scope["stage_digest"][7:]
            stage_path.write_bytes(original)
            self.assertEqual(run()["mesh"]["rc"], 4)

