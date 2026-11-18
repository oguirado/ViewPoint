# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT.parent.parent / "vp-direct" / "schemas"
RECORDS = """
video_source frame keyframe_set ui_element action screen transition workflow software_model
spec reimpl_plan scaffold_receipt verification report game.entity game.event
game.physics game.level_map game.model game.replay_metrics telemetry
""".split()
OPS = """
ingest sample segment model spec plan scaffold verify report live.tap
game.model game.physics game.replay
""".split()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256((canonical + "\n").encode()).hexdigest()


def example(name):
    return read(ROOT / "tests" / "examples" / (name + ".json"))


class ContractCase(unittest.TestCase):
    def setUp(self):
        files = list(ROOT.glob("*.schema.json"))
        external = read(SCHEMAS / "vp.artifact_ref_v1.schema.json")
        docs = [read(path) for path in files] + [external]
        self.registry = Registry().with_resources(
            (doc["$id"], Resource.from_contents(doc)) for doc in docs
        ).crawl()

    def validator(self, name):
        resource = self.registry.contents("urn:" + name + ":v1")
        return Draft202012Validator(resource, registry=self.registry)


class ContractTests(ContractCase):
    def check_pair(self, name):
        validator = self.validator(name)
        Draft202012Validator.check_schema(validator.schema)
        pair = example(name)
        validator.validate(pair["valid"])
        self.assertFalse(validator.is_valid(pair["invalid"]))

    def test_inventory_and_examples(self):
        expected = {"rea." + name for name in RECORDS} | {"vp_rea_call.args"}
        docs = [read(p) for p in ROOT.glob("*.schema.json")]
        self.assertEqual({doc["title"] for doc in docs}, {name + "/v1" for name in expected})
        for name in sorted(expected):
            with self.subTest(name=name):
                self.check_pair(name)
                if name.startswith("rea.") and name != "rea.telemetry":
                    record = example(name)["valid"]
                    witness_key = "evidence" if "evidence" in record else "provenance"
                    del record[witness_key]
                    self.assertFalse(self.validator(name).is_valid(record))

    def test_schema_patterns_compile(self):
        for path in sorted(ROOT.glob("*.schema.json")):
            pending = [Resource.from_contents(read(path))]
            while pending:
                resource = pending.pop()
                pending.extend(resource.subresources())
                schema = resource.contents
                if not isinstance(schema, dict) or "pattern" not in schema:
                    continue
                pattern = schema["pattern"]
                with self.subTest(file=path.name, pattern=pattern, engine="re"):
                    re.compile(pattern)
                with self.subTest(file=path.name, pattern=pattern, engine="jsonschema"):
                    keyword = {"type": "string", "pattern": pattern}
                    Draft202012Validator.check_schema(keyword)
                    Draft202012Validator(keyword).is_valid("")

    def test_observation_requires_frame_and_evidence(self):
        validator = self.validator("rea.ui_element")
        item = example("rea.ui_element")["valid"]
        self.assertEqual(item["provenance"]["label"], "observed")
        del item["provenance"]["frame_sha256"]
        self.assertFalse(validator.is_valid(item))
        degraded = example("rea.ui_element")["degraded"]
        validator.validate(degraded)
        self.assertFalse(validator.is_valid(dict(degraded, text="imagined text")))
        self.assertFalse(validator.is_valid(dict(degraded, degraded_reason=None)))

    def test_registry_and_dispatch_contract(self):
        table = read(ROOT / "vp_rea_call.op_table.json")
        self.assertEqual(table["tool"], "vp_rea_call")
        self.assertEqual(set(table["op_table"]), {"rea." + op for op in OPS})
        codes = set(table["error_codes"])
        self.assertEqual(codes, {"-32601", "-32602", "HUMAN_NEEDED", "ROUTE_ABSTAIN"})
        self.assertTrue(codes <= set(read(SCHEMAS / "error_codes.json")))
        factory = read(SCHEMAS / "adapter_ops.json")["vp_factory_call"]["ops"]
        factory_row = next(iter(factory.values()))
        allowed_risks = read(SCHEMAS / "vp.capability_v1.schema.json")["properties"]["risk_class"]["enum"]
        envelope = self.validator("vp_rea_call.args")
        for op, row in table["op_table"].items():
            with self.subTest(op=op):
                self.assertEqual(set(row), set(factory_row))
                self.assertIsInstance(row["contract_source"], str)
                source = f"components/video-rea/contracts/vp_rea_call.args_v1.schema.json#/$defs/{op}.args"
                self.assertEqual(row["contract_source"], source)
                self.assertIn(row["risk_class"], allowed_risks)
                self.assertIs(row["supported"], False)
                self.assertIs(row["operator_only"], False)
                self.assertEqual(row["binding"], {"kind": "none", "name": "", "source": None})
                if op == "rea.scaffold":
                    self.assertEqual(row["risk_class"], "preparar")
                else:
                    self.assertIn(row["risk_class"], {"observar", "leer"})
                for field in ("args_schema", "reply_schema"):
                    suffix = "args" if field == "args_schema" else "result"
                    self.assertEqual(row[field], f"vp_rea_call.{op}.{suffix}/v1")
                    name = row[field].removesuffix("/v1")
                    self.check_pair(name)
                    self.assertEqual(digest(self.validator(name).schema), row[field + "_sha256"])
                args = example(row["args_schema"].removesuffix("/v1"))
                self.assertEqual(args["valid"]["op"], op)
                envelope.validate(args["valid"])
                self.assertFalse(envelope.is_valid(args["invalid"]))
        self.assertFalse(envelope.is_valid({"op": "rea.unknown"}))

    def test_physics_units_and_replay_mode(self):
        physics = example("rea.game.physics")["valid"]
        validator = self.validator("rea.game.physics")
        validator.validate(dict(physics, pixel_to_world={"value": None, "reason": "no scale witness"}))
        self.assertFalse(validator.is_valid(dict(physics, gravity={"value": 9.8, "unit": "m/s^2"})))
        metrics = self.validator("rea.game.replay_metrics")
        cases = example("rea.game.replay_metrics")
        metrics.validate(cases["distribution"])
        self.assertFalse(metrics.is_valid(dict(cases["valid"], distribution_match=True)))

    def test_telemetry_optional_measurements(self):
        validator = self.validator("rea.telemetry")
        event = example("rea.telemetry")["valid"]
        validator.validate(dict(event, dram_bytes=None))
        self.assertFalse(validator.is_valid(dict(event, copy_bytes=1, source=None)))

    def test_pin_gate(self):
        from contracts import pins
        manifest = read(ROOT / "PINS.json")
        for name, pinned in manifest["files"].items():
            with self.subTest(file=name):
                self.assertEqual(digest(read(ROOT / name)), pinned)
        self.assertEqual(pins.check(ROOT), 0)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            for source in ROOT.glob("*.json"):
                shutil.copyfile(source, target / source.name)
            path = target / "rea.frame_v1.schema.json"
            original = path.read_bytes()
            self.assertIn(b"rea.frame/v1", original)
            path.write_bytes(original.replace(b"rea.frame/v1", b"rea.framE/v1", 1))
            self.assertEqual(pins.check(target), 5)
            path.write_bytes(original)
            self.assertEqual(pins.check(target), 0)
            table = target / "vp_rea_call.op_table.json"
            table.write_text(table.read_text().replace('"supported": false', '"supported": true', 1))
            self.assertEqual(pins.check(target), 5)
            table.unlink()
            self.assertEqual(pins.check(target), 5)

