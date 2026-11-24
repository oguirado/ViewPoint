# SPDX-License-Identifier: AGPL-3.0-only
"""Writer-authored structural checks. Independent admission and custody run elsewhere."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from contracts.ext.verify import Catalog, ContractError, canonical_digest, decode, sha256


COMPONENT = Path(__file__).resolve().parents[2]
EXT = COMPONENT / "contracts/ext"
DIGEST = os.environ["REA_EXTENSION_SHA256"]
GENERATION = EXT / "generations" / DIGEST
STAGE = EXT / ".stage"


class ExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("REA_CHECK_STAGED") == "1":
            cls.catalog = Catalog._staged(COMPONENT, STAGE, DIGEST)
        else:
            cls.catalog = Catalog(COMPONENT, DIGEST)
        cls.fixture = decode((cls.catalog.generation / "fixtures.json").read_bytes())
        cls.records = cls.fixture["records"]
        cls.contrasts = {row["id"]: row["expected"] for row in cls.fixture["contrasts"]}

    def test_baseline_provenance_uses_symbolic_roots(self):
        baseline = decode((self.catalog.generation / "baseline.json").read_bytes())
        evidence_paths = [
            *(row["receipt_path"] for row in baseline["accepted_synthetic_summaries"]),
            *(row["path"] for row in baseline["patches"].values()),
            baseline["prediction_format"]["path"],
        ]
        self.assertTrue(all(path.startswith("${REA_BUILD_ROOT}/out/") for path in evidence_paths))
        self.assertTrue(baseline["repo"] == "${REA_BASELINE_REPO}")
        self.assertTrue(baseline["index"]["path"] == "${REA_BASELINE_GIT_DIR}/index")

    def test_readiness_provenance_uses_symbolic_roots(self):
        readiness = decode((self.catalog.generation / "readiness.json").read_bytes())
        runtime = readiness["runtime"]
        executable = "${NEWTON_PY_VENV}/bin/newton"
        self.assertTrue(runtime["executable"] == executable)
        self.assertTrue(runtime["invocation"][0] == executable)
        self.assertTrue(runtime["invocation"][2] == "${NEWTON_PY_VENV}/pyvenv.cfg")
        self.assertTrue(runtime["footer"].startswith("# [newton read: ${NEWTON_PY_VENV}/"))
        for route in readiness["routes"]:
            self.assertTrue(route["invocation"][0] == executable)
            self.assertTrue(route["citation"].startswith("${AGENTS_SKILLS}/"))
        for field in ("path", "scoped_wrapper"):
            self.assertTrue(readiness["workflow"][field].startswith("${REA_BUILD_ROOT}/"))
        for field in ("scorer", "runner"):
            self.assertTrue(readiness["public_custody_sources"][field]["path"].startswith(
                "${REA_BUILD_ROOT}/out/custody/"
            ))

    def refusal(self, contrast, action, reason):
        with self.assertRaises(ContractError) as error:
            action()
        self.assertEqual(error.exception.code, self.contrasts[contrast])
        self.assertIn(reason, str(error.exception))

    def record(self, mode, name):
        return deepcopy(self.records[self.fixture["routes"][mode][name]["sha256"]])

    def close(self, name, value):
        self.catalog.validate_closure(name, value, self.records)

    def test_legacy_inventory_both_profiles(self):
        base = COMPONENT / "contracts/tests/examples"
        examples = {p.stem + "/v1": decode(p.read_bytes()) for p in base.glob("*.json")}
        examples["rea.game.scene/v1"] = decode(
            (COMPONENT / "game/scene/scene.fixtures.json").read_bytes())
        expected = len(self.catalog.central_table["op_table"]) * 2 + 23
        self.assertEqual(len(examples), expected)
        for profile in ("legacy", "local_fixture"):
            for schema, pair in sorted(examples.items()):
                with self.subTest(profile=profile, schema=schema):
                    self.catalog.validate(schema, pair["valid"], profile)
                    with self.assertRaisesRegex(ContractError, "^SCHEMA:"):
                        self.catalog.validate(schema, pair["invalid"], profile)
        for operation in self.catalog.central_table["op_table"].values():
            self.assertEqual(operation["binding"], {"kind": "none", "name": "", "source": None})
            self.assertIs(operation["supported"], False)

    def test_source_routes_and_opaque_round_trip(self):
        for mode in ("video-only", "binary-only", "video+binary"):
            for name in self.fixture["routes"][mode]:
                row = self.record(mode, name)
                with self.subTest(mode=mode, name=name):
                    self.close(name, row)
                    if mode == "binary-only":
                        self.assertTrue(all(witness["origin"] == "binary"
                                            for witness in row.get("evidence", [])))
        for ref in self.fixture["standalone"]:
            with self.subTest(name=ref["schema"]):
                self.close(ref["schema"], self.records[ref["sha256"]])
        lift = next(row for row in self.records.values() if row.get("schema") == "rea.binary.lift/v1")
        self.assertEqual(decode(json.dumps(lift).encode())["opaque"], lift["opaque"])
        self.assertEqual(len(lift["opaque"]), 1)
        inventory = self.records[lift["artifact"]["sha256"]]
        raw = self.fixture["literal_artifacts"][inventory["source"]["path"]].encode()
        self.assertEqual(sha256(raw), inventory["source"]["sha256"])
        self.assertEqual(len(raw), inventory["byte_size"])
        for member in inventory["members"]:
            span = member["range"]
            offset = span["offset"]
            self.assertEqual(sha256(raw[offset:offset + span["length"]]), span["sha256"])

    def test_abi_and_every_local_selector(self):
        self.catalog.validate_exchange(**self.fixture["exchange"])
        case = self.fixture["operation"]
        self.catalog.validate_operation(case["args"], case["reply"],
                                        approved_inputs=case["admitted_inputs"], records=self.records)
        slot_manifest = {entry["slot"]: entry for entry in self.catalog.slot_manifest["slots"]}
        for op, operation in self.catalog.local_table["op_table"].items():
            row = slot_manifest[operation["slot"]]

            def artifact(role, suffix):
                schema = role["schemas"][0] if role["schemas"] else None
                return {"role": role["role"], "path": suffix, "media_type": "application/json",
                        "sha256": hashlib.sha256(suffix.encode()).hexdigest(), "record_schema": schema}

            inputs = [artifact(role, f"in-{role['role']}") for role in row["inputs"] if role["required"]]
            if row["exclusive_inputs"]:
                selected = row["exclusive_inputs"][0]
                inputs.extend(artifact(role, f"in-{selected}") for role in row["inputs"]
                              if role["role"] == selected)
            outputs = [artifact(role, f"out-{role['role']}")
                       for role in row["outputs"] if role["required"]]
            args = {"op": op, "profile": "offline_fixture", "goal_id": "typed-fixture", "inputs": inputs}
            if op == "rea.scaffold":
                args["output_dir"] = "/fixture/output"
            reply = {"op": op, "profile": "offline_fixture", "artifact_refs": outputs,
                     "receipt": case["reply"]["receipt"]}
            with self.subTest(op=op):
                self.catalog.validate_operation(args, reply, approved_inputs=inputs)

    def test_record_and_route_contrasts(self):
        model_name = "rea.software_model/v2"
        evidence = self.record("binary-only", "rea.game.evidence/v1")
        alias = {**evidence, "schema": "rea.game_evidence/v1"}
        self.refusal("alias", lambda: self.close("rea.game.evidence/v1", alias), "$.schema")
        binary = self.record("binary-only", model_name)
        self.refusal("legacy-binary", lambda: self.close("rea.software_model/v1", binary), "$.schema")
        fake_screen = {**binary, "screens": [{"schema": "rea.screen/v1"}]}
        self.refusal("invented-screen", lambda: self.close(model_name, fake_screen), "additionalProperties")
        track = deepcopy(next(row for row in self.records.values()
                              if row.get("schema") == "rea.game.sprite_track/v1"))
        track["samples"][0]["witness"]["occurrence"]["frame_index"] += 1
        self.refusal("invented-frame", lambda: self.close(track["schema"], track), "invented frame provenance")
        report = self.record("video+binary", "rea.report/v2")
        report["mode"] = "video-only"
        report["evidence"] = self.record("video-only", "rea.report/v2")["evidence"]
        self.refusal("wrong-branch", lambda: self.close(report["schema"], report), "changed source mode")
        for field, record in (("entities", "entity"), ("events", "event"), ("trace", "replay_trace")):
            broken = self.record("video-only", model_name)
            ref = {"schema": f"rea.game.{record}/v1", "sha256": "1" * 64}
            broken[field] = ref if field == "trace" else [ref]
            with self.subTest(field=field):
                name = "trace" if field == "trace" else record
                self.refusal("broken-" + name, lambda: self.close(model_name, broken), ref["schema"])
        dual = self.record("video+binary", "rea.game.evidence/v1")
        dual["facts"][1]["join_key"]["build_sha256"] = "1" * 64
        self.refusal("foreign-build", lambda: self.close(dual["schema"], dual), "join differs")
        lift_ref = evidence["lifts"][0]
        lift = deepcopy(self.records[lift_ref["sha256"]])
        lift["opaque"] = []
        self.refusal("dropped-opaque-member", lambda: self.close(lift["schema"], lift),
                     "opaque member was discarded")

    def test_transport_contrasts(self):
        exchange = deepcopy(self.fixture["exchange"])
        exchange["result"]["provider_revision"] = "foreign-revision"
        self.refusal("wrong-provider-revision", lambda: self.catalog.validate_exchange(**exchange), "revision")
        exchange = deepcopy(self.fixture["exchange"])
        exchange["result"]["outputs"] = []
        self.refusal("missing-ok-output", lambda: self.catalog.validate_exchange(**exchange), "$.outputs")
        case = self.fixture["operation"]
        private = deepcopy(case["args"])
        private["inputs"][0]["record_schema"] = "rea.custody.binary_truth/v1"
        self.refusal("private-role", lambda: self.catalog.validate_operation(private), "$.inputs[0]")
        masquerade = deepcopy(case["args"])
        masquerade["inputs"][0]["path"] = "custody-private/synthetic-truth.json"
        self.refusal("private-path", lambda: self.catalog.validate_operation(
            masquerade, approved_inputs=case["admitted_inputs"]), "independently admitted")
        with (COMPONENT / "contracts/tests/examples/vp_rea_call.rea.ingest.args.json").open() as handle:
            central = json.load(handle)["valid"]
        central["op"] = "rea.binary.ingest"
        self.refusal("central-selector", lambda: self.catalog.validate(
            "vp_rea_call.args/v1", central, "legacy"), "$.op")

    def test_publication_contrasts_in_isolated_copy(self):
        cases = {
            "base-parent-changed": "video-rea/contracts/PINS.json",
            "scene-parent-changed": "video-rea/game/scene/PINS.json",
            "stale-pointer": "video-rea/contracts/ext/PINS.json",
            "mixed-generation": f"video-rea/contracts/ext/generations/{DIGEST}/rea.spec_v2.schema.json",
        }
        with tempfile.TemporaryDirectory(prefix="rea-extension-tests-") as temp:
            root = Path(temp)
            component = root / "video-rea"
            base = component / "contracts"
            base.mkdir(parents=True)
            ext = base / "ext"
            generation = ext / "generations" / DIGEST
            shutil.copytree(self.catalog.generation, generation)
            shutil.copy2(generation / "PINS.json", ext / "PINS.json")
            shutil.copy2(EXT / "verify.py", ext / "verify.py")
            for p in [*Path(COMPONENT / "contracts").glob("*.schema.json"),
                      COMPONENT / "contracts/PINS.json", COMPONENT / "contracts/vp_rea_call.op_table.json"]:
                shutil.copy2(p, base / p.name)
            scene = component / "game/scene"
            scene.mkdir(parents=True)
            for name in ("PINS.json", "scene.schema.json", "scene.fixtures.json"):
                shutil.copy2(COMPONENT / "game/scene" / name, scene / name)
            schema = root / "vp-direct/schemas/vp.artifact_ref_v1.schema.json"
            schema.parent.mkdir(parents=True)
            shutil.copy2(COMPONENT.parent / "vp-direct/schemas" / schema.name, schema)
            self.assertEqual(Catalog(component, DIGEST).extension_sha256, DIGEST)
            for name, relative in cases.items():
                path = root / relative
                original = path.read_bytes()
                changed = decode(original)
                if name == "mixed-generation":
                    changed["description"] = "Schema bytes from a different generation."
                changed_bytes = json.dumps(changed, indent=1).encode()
                self.assertNotEqual(changed_bytes, original)
                if name == "stale-pointer":
                    other = generation.parent / sha256(changed_bytes)
                    shutil.copytree(generation, other)
                    (other / "PINS.json").write_bytes(changed_bytes)
                    (ext / "PINS.json").write_bytes(changed_bytes)
                    self.assertEqual(Catalog(component, other.name).extension_sha256, other.name)
                path.write_bytes(changed_bytes)
                with self.subTest(name=name):
                    self.refusal(name, lambda: Catalog(component, DIGEST), path.name)
                path.write_bytes(original)
            self.assertEqual(Catalog(component, DIGEST).extension_sha256, DIGEST)

    def test_phase_score_is_bounded_and_fail_closed(self):
        name = "rea.custody.phase_policy/v1"
        same = "2" * 64
        policy = {
            "schema": name, "confidentiality": "custody_policy", "phase": "P", "parent_p_sha256": None,
            "extension_sha256": DIGEST, "scorer_sha256": same, "fixture_manifest_sha256": same,
            "dependency_receipt_sha256s": [same], "prediction_format_sha256": same,
            "mandatory_cases": ["case"], "critical_mutants": ["contrast"],
            "limits": {"case_timeout_s": 120, "wall_timeout_s": 5400, "independent_cases": 8},
            "thresholds": [{"metric": "authored structural check", "comparison": "eq", "value": 1}],
            "custody_order": ["freeze", "draw_distinct_seeds", "plant_canary",
                              "run_observations_actions_only", "seal_predictions", "regenerate_truth", "score"],
        }
        bindings = {key: policy[key] for key in ("phase", "parent_p_sha256", "extension_sha256",
                                                 "scorer_sha256", "fixture_manifest_sha256",
                                                 "dependency_receipt_sha256s")}
        policy_digest = canonical_digest(policy)
        receipt = {
            "schema": "rea.custody.phase_receipt/v1", "confidentiality": "custody_score", **bindings,
            "policy": {"schema": name, "sha256": policy_digest},
            "candidate_manifest_sha256": same, "candidate_tree": "2" * 40,
            "sealed_predictions_sha256": same, "pilot_receipt_sha256": same, "status": "PASS",
            "score": {"attempted": 1, "eligible": 1, "S": 1, "F": 0, "U": 0, "N": 1,
                      "interval": [1, 1], "exclusion_reasons": []},
            "mandatory_cases": [{"id": "case", "passed": True}],
            "critical_mutants": [{"id": "contrast", "passed": True}], "K": 1, "M": 1,
        }
        records = {policy_digest: policy}
        self.catalog.validate_closure(receipt["schema"], receipt, records, custody=True)
        with self.assertRaisesRegex(ContractError, "^PRIVACY:"):
            self.catalog.validate_closure(receipt["schema"], receipt, records)
        wrong_phase = {**receipt, "phase": "F", "parent_p_sha256": same}
        with self.assertRaisesRegex(ContractError, "^PHASE:"):
            self.catalog.validate_closure(receipt["schema"], wrong_phase, records, custody=True)
        empty = deepcopy(receipt)
        empty["score"].update(eligible=0, S=0, N=0, interval=None)
        with self.assertRaisesRegex(ContractError, "^SCORE: phase PASS"):
            self.catalog.validate_closure(receipt["schema"], empty, records, custody=True)
        surviving = {**receipt, "K": 0, "critical_mutants": [{"id": "contrast", "passed": False}]}
        with self.assertRaisesRegex(ContractError, "^SCORE: phase PASS"):
            self.catalog.validate_closure(receipt["schema"], surviving, records, custody=True)


if __name__ == "__main__":
    unittest.main()
