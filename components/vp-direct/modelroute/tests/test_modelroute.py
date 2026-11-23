from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import ValidationError


HERE = Path(__file__).resolve().parent
VP_DIRECT = HERE.parents[1]


def load_module(name, path):
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


m2 = load_module("vp_modelroute", HERE.parent / "__init__.py")
m1 = load_module("vp_modelroute_fixture_m1", VP_DIRECT / "factory/manifest/manifest.py")
c5 = load_module("vp_modelroute_fixture_c5", VP_DIRECT / "router/router.py")

TOOL = "vp_factory_call"
GENERATE = "af.generate"
RIG = "af.rig"
TRELLIS = "TRELLIS.2-4B"
PROFILE = "fixed image-to-mesh fixture"


class ModelRouteTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=HERE)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.pins = set()
        self.measurements = []
        args, self.control, self.levels, frozen = m1._contracts()
        self.args_digest = sha256(m2.canonical_bytes(args)).hexdigest()
        self.catalog = deepcopy(frozen)
        self.source = {**self.write_evidence("binding.txt", b"Synthetic binding only.\n"),
                       "line_start": 1, "line_end": 1}
        self.catalog[GENERATE].update(supported=True, binding={
            "kind": "factory_cmd", "name": GENERATE, "source": self.source,
        })
        self.trellis = self.declaration(TRELLIS)
        self.rig = self.declaration("UniRig", op=RIG, qualified=False)
        self.fact(self.trellis, 5)

    def write_evidence(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return {"path": name, "sha256": sha256(path.read_bytes()).hexdigest()}

    def declaration(self, name, *, op=GENERATE, qualified=True):
        weights = sha256(f"fixture:{name}".encode()).hexdigest()
        receipt = None
        if qualified:
            body = m2.canonical_bytes({"model": name, "weights": weights})
            receipt = self.write_evidence(f"qualification-{weights}.json", body)
            self.pins.add((name, weights, receipt["path"], receipt["sha256"]))
        policy = self.catalog[op]
        supported = qualified and policy["supported"]
        row = {key: deepcopy(policy[key]) for key in (
            "operator_only", "risk_class", "binding", "args_schema_sha256", "reply_schema_sha256",
        )}
        return {
            "schema": m1.CAPABILITY, "tool": TOOL, "cli": "factory", "json_op": TOOL,
            "origin": "factory", "args_schema_sha256": self.args_digest, "ops": [op],
            "op_table": [dict(row, op=op, supported=supported)],
            "risk_class": row["risk_class"], "operator_only": row["operator_only"],
            "supported": supported, "source": [self.source],
            "route": {"verbs": [op.removeprefix("af.")], "objects": ["mesh"],
                      "argv_template": "mvctl factory <op> --goal-id <goal_id>"},
            "provider": {"kind": "local_model", "model": {
                "name": name, "role": "synthetic fixture provider",
                "weights": {"format": "safetensors", "bytes_on_disk": 1,
                            "tree_sha256": weights, "locator": "af://fixture/weights"},
                "deps": [], "device": "gpu", "license": {"id": "fixture", "territory_ok": True},
                "qualification": {"state": "runnable" if qualified else "unqualified",
                                  "receipt": receipt, "reason": "isolated test fixture"},
                "measured": [], "residency": {"mode": "cold_process", "lease": "gpu_exclusive",
                                              "resident_bytes": None},
            }},
        }

    @staticmethod
    def model(entry):
        return entry["provider"]["model"]

    def fact(self, entry, value, *, tag="MEASURED", profile=PROFILE):
        model = self.model(entry)
        body = {"metric": "wall_s", "value": value, "tag": tag, "profile": profile}
        name = f"sample-{len(self.measurements)}.json"
        source = self.write_evidence(name, m2.canonical_bytes(body))
        fact = dict(body, source=source)
        model["measured"].append(fact)
        observed = json.loads((self.root / name).read_bytes())
        self.measurements.append(m2.VerifiedMeasurement(
            model["name"], model["weights"]["tree_sha256"], dict(observed, source=source), 1, 1,
        ))
        return fact

    def manifest(self, entry, *, synthetic_binding=True):
        path = self.root / "manifest.json"
        path.write_bytes(m2.canonical_bytes({"schema": m1.MANIFEST, "origin": "factory",
                                             "capabilities": [entry]}))
        if not synthetic_binding:
            return m1.load_manifest(path, asset_factory_root=self.root,
                                    verified_qualifications=self.pins).registry_entry()
        args, _, _, _ = m1._contracts()
        with patch.object(m1, "_contracts", return_value=(args, self.control, self.levels, self.catalog)):
            loaded = m1.load_manifest(path, asset_factory_root=self.root,
                                      verified_qualifications=self.pins)
        return loaded.registry_entry()

    def index(self, entries=None, **overrides):
        inputs = {"verified_qualifications": self.pins, "verified_measurements": self.measurements}
        if entries is None:
            entries = [self.trellis, self.rig]
        return m2.ModelIndex(entries, **dict(inputs, **overrides))

    def assert_model(self, decision, name=TRELLIS):
        self.assertIsNotNone(decision.candidate, decision)
        self.assertEqual(decision.candidate.provider["model"]["name"], name)

    def assert_trellis_wins(self, entries):
        decision = self.index(entries).select(TOOL, GENERATE, metric="wall_s", profile=PROFILE)
        self.assert_model(decision)

    def test_held_out_mesh_intent_selects_trellis_with_verified_citations(self):
        effective = self.manifest(self.trellis)
        (intent,) = c5._rank([effective], "generate a mesh from this image")
        self.assertEqual(intent.key, (TOOL, GENERATE))
        with patch.object(c5._validation, "_adapter_rows", return_value={TOOL: {"ops": self.catalog}}):
            self.assertEqual(c5._static_policy(intent, {"caller": "agent"}), (True, None))
        decision = self.index([effective]).select(*intent.key)
        self.assert_model(decision)
        self.assertEqual((decision.candidate.tool, decision.candidate.op), (TOOL, GENERATE))
        pixels = b"P6\n1 1\n255\n\x01\x02\x03"
        source = self.write_evidence("input.ppm", pixels)
        artifact = {"schema": "vp.artifact_ref/v1", "locator": (self.root / source["path"]).as_uri(),
                    "sha256": source["sha256"], "bytes": len(pixels),
                    "media_type": "image/x-portable-pixmap", "stage": "input", "copy": "file"}
        request = c5._request(intent, {"goal_id": "fixture-goal", "inputs": [artifact]},
                              {"caller": "agent", "agent_instance": "fixture"}, "fixture-request", True)
        self.assertEqual((request["op"], request["args"]["op"]), (TOOL, GENERATE))
        (fact,) = decision.candidate.provider["model"]["measured"]
        (citation,) = decision.candidate.citations
        data = (self.root / citation["path"]).read_bytes()
        self.assertEqual(citation["sha256"], sha256(data).hexdigest())
        self.assertEqual((citation["line_start"], citation["line_end"]), (1, 1))
        self.assertEqual(json.loads(data), {key: value for key, value in fact.items() if key != "source"})
        self.assertEqual(fact["tag"], "MEASURED")

    def test_provider_choice_and_fact_digest_are_byte_stable(self):
        slower = self.declaration("Second fixture model")
        self.fact(slower, 8)
        self.fact(self.trellis, 4)
        entries = [self.trellis, slower, self.rig]
        index = self.index(entries)
        reversed_entries = deepcopy(entries[::-1])
        self.model(reversed_entries[-1])["measured"].reverse()
        rebuilt = self.index(reversed_entries, verified_measurements=reversed(self.measurements))
        self.assertEqual(index.model_facts_sha256, rebuilt.model_facts_sha256)
        def compute(view):
            (intent,) = c5._rank([self.trellis], "generate a mesh from this image")
            return m2.canonical_bytes(asdict(view.select(*intent.key, metric="wall_s", profile=PROFILE)))

        expected = compute(index)
        self.assertEqual(compute(index), expected)
        self.assertEqual(compute(rebuilt), expected)
        self.assertNotEqual(self.index([self.trellis]).model_facts_sha256, index.model_facts_sha256)
        self.model(self.trellis)["measured"].clear()
        index.select(TOOL, GENERATE, model_name=TRELLIS).candidate.provider["model"].clear()
        self.assertEqual(compute(index), expected)

    def test_held_out_auto_rig_abstains_without_forge_pointer(self):
        effective = self.manifest(self.rig)
        index = self.index([effective])
        for query in ("auto-rig this mesh", "auto-rig this mesh with UniRig"):
            with self.subTest(query=query):
                (intent,) = c5._rank([effective], query)
                answer = json.loads(index.select(*intent.key).abstention_bytes("1" * 64))
                self.assertEqual(answer, {
                    "verdict": "ABSTAIN", "code": "ROUTE_ABSTAIN", "reason": "policy_denied",
                    "detail": "model_unqualified", "index_sha256": "1" * 64, "candidates": [],
                })

    def test_missing_binding_and_independent_qualification_are_refused(self):
        self.assertEqual(self.index(verified_qualifications=()).select(TOOL, GENERATE).detail,
                         "model_unqualified")
        self.assertEqual(self.index().select(TOOL, GENERATE, model_name="Unknown").detail,
                         "model_unqualified")
        declaration = deepcopy(self.trellis)
        _, _, _, catalog = m1._contracts()
        row = declaration["op_table"][0]
        row.update(supported=False, binding=catalog[GENERATE]["binding"])
        declaration["supported"] = False
        effective = self.manifest(declaration, synthetic_binding=False)
        self.assertEqual(self.index([effective]).select(TOOL, GENERATE).detail, "op_unbound")
        with self.assertRaises(ValidationError):
            self.model(declaration)["qualification"]["receipt"] = None
            self.index([declaration])

    def test_qualification_pin_cannot_be_reused_for_other_weights(self):
        changed = deepcopy(self.trellis)
        self.model(changed)["weights"]["tree_sha256"] = "a" * 64
        self.assertEqual(self.index([changed]).select(TOOL, GENERATE).detail, "model_unqualified")

    def test_unattested_value_with_real_source_pin_cannot_rank(self):
        slower = self.declaration("Second fixture model")
        fact = self.fact(slower, 8)
        fact["value"] = 0
        index = self.index([self.trellis, slower])
        candidate = index.select(TOOL, GENERATE, model_name=self.model(slower)["name"]).candidate
        self.assertEqual(candidate.provider["model"]["measured"], [])
        self.assertEqual(candidate.citations, ())
        self.assertEqual(index.select(TOOL, GENERATE, metric="wall_s", profile=PROFILE).reason,
                         "ambiguous")

    def test_rank_requires_comparable_measurements_and_rejects_ties(self):
        other = self.declaration("Second fixture model")
        self.fact(other, 5)
        self.fact(other, 0, profile="different workload")
        index = self.index([self.trellis, other])
        self.assertEqual(index.select(TOOL, GENERATE).reason, "ambiguous")
        self.assertEqual(index.select(TOOL, GENERATE, metric="wall_s", profile=PROFILE).reason,
                         "ambiguous")
        self.assertEqual(index.select(TOOL, GENERATE, metric="wall_s", profile="different workload").reason,
                         "ambiguous")

    def test_target_and_cited_facts_cannot_rank_or_change_digest(self):
        other = self.declaration("Second fixture model")
        self.fact(other, 8)
        entries = [self.trellis, other]
        before = self.index(entries).model_facts_sha256
        self.fact(other, 0, tag="TARGET")
        self.fact(other, 0, tag="CITED")
        self.assertEqual(self.index(entries).model_facts_sha256, before)
        self.assert_trellis_wins(entries)

    def test_excluded_model_never_wins_or_enters_fact_digest(self):
        excluded = self.declaration("Hunyuan3D-2.1", qualified=False)
        self.model(excluded)["qualification"]["state"] = "excluded"
        self.model(excluded)["license"]["territory_ok"] = False
        self.fact(excluded, 0)
        index = self.index([self.trellis, excluded])
        self.assertEqual(index.model_facts_sha256, self.index([self.trellis]).model_facts_sha256)
        self.assert_model(index.select(TOOL, GENERATE, metric="wall_s", profile=PROFILE))
        self.assertEqual(index.select(TOOL, GENERATE, model_name="Hunyuan3D-2.1").detail, "model_excluded")

    def test_mutant_ranking_target_fails_the_provider_oracle(self):
        other = self.declaration("Second fixture model")
        self.fact(other, 8)
        target = self.fact(other, 0, tag="TARGET")
        entries = [self.trellis, other]
        self.assert_trellis_wins(entries)
        original = m2._model_facts

        def include_target(model, citations):
            found = original(model, citations)
            if target in model["measured"]:
                found.append((target, dict(target["source"], line_start=1, line_end=1)))
            return found

        with patch.object(m2, "_model_facts", side_effect=include_target) as mutant:
            with self.assertRaisesRegex(AssertionError, "Second fixture model"):
                self.assert_trellis_wins(entries)
            self.assertEqual(mutant.call_count, 2)

    def test_mutant_routing_excluded_alias_fails_the_provider_oracle(self):
        excluded = self.declaration("Hunyuan3D_2_1")
        self.fact(excluded, 0)
        entries = [self.trellis, excluded]
        self.assert_trellis_wins(entries)
        with patch.object(m2, "_excluded", return_value=False) as mutant:
            with self.assertRaisesRegex(AssertionError, "Hunyuan3D_2_1"):
                self.assert_trellis_wins(entries)
            mutant.assert_any_call(self.model(excluded))
