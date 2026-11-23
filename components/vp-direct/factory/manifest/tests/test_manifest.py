from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError


HERE = Path(__file__).resolve().parent
spec = spec_from_file_location("factory_manifest", HERE.parent / "manifest.py")
m1 = module_from_spec(spec)
spec.loader.exec_module(m1)


def digest(data):
    return sha256((json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()


def provider(name="TRELLIS.2-4B", receipt=None):
    return {
        "kind": "local_model",
        "model": {
            "name": name, "role": "image to mesh",
            "weights": {
                "format": "safetensors", "bytes_on_disk": 16, "tree_sha256": "1" * 64,
                "locator": "af://fixtures/model",
            },
            "deps": [], "device": "gpu",
            "license": {"id": "Fixture", "territory_ok": True},
            "qualification": {"state": "runnable", "receipt": receipt, "reason": "Synthetic"},
            "measured": [],
            "residency": {"mode": "cold_process", "lease": "gpu_exclusive", "resident_bytes": None},
        },
    }


def request(op, caller="agent"):
    return {
        "schema": m1.REQUEST, "request_id": "fixture-request", "op": m1.TOOL,
        "args": {"op": op, "goal_id": "fixture-goal", "inputs": []},
        "context": {"caller": caller, "agent_instance": "fixture-agent"}, "dry_run": True,
    }


class FactoryManifestTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=HERE)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "manifest.json"
        self.args, self.control, self.levels, self.catalog = m1._contracts()
        self.source = {"path": "fixture.py", "line_start": 1, "line_end": 1, "sha256": "2" * 64}
        self.receipt = {"path": "fixture-receipt.json", "sha256": "3" * 64}
        self.pin = ("TRELLIS.2-4B", "1" * 64, self.receipt["path"], self.receipt["sha256"])

    def entry(self, ops, *, catalog=None):
        if catalog is None:
            catalog = self.catalog
        rows = [
            {"op": op, **{key: deepcopy(catalog[op][key]) for key in (
                "supported", "operator_only", "risk_class", "args_schema_sha256",
                "reply_schema_sha256", "binding",
            )}}
            for op in ops
        ]
        return {
            "schema": m1.CAPABILITY, "tool": m1.TOOL, "cli": m1.CLI, "json_op": m1.TOOL,
            "origin": "factory", "args_schema_sha256": digest(self.args), "ops": list(ops),
            "op_table": rows, "risk_class": max((row["risk_class"] for row in rows), key=self.levels.index),
            "operator_only": all(row["operator_only"] for row in rows),
            "supported": any(row["supported"] for row in rows),
            "route": {"verbs": ["generate"], "objects": ["mesh"], "argv_template": "mvctl factory"},
            "source": [self.source],
        }

    def write(self, *entries):
        self.path.write_text(json.dumps({
            "schema": m1.MANIFEST, "origin": "factory", "capabilities": entries,
        }), encoding="utf-8")

    def load(self, *pins):
        return m1.load_manifest(self.path, asset_factory_root=self.root, verified_qualifications=pins)

    def bound(self, risk="preparar"):
        catalog = deepcopy(self.catalog)
        catalog["af.generate"].update(
            supported=True, risk_class=risk,
            binding={"kind": "factory_cmd", "name": "fixture", "source": self.source},
        )
        return catalog

    def test_manifest_projection_parity_and_flat_mcp_schema(self):
        ops = ["vqa3d.gate", "af.registry.resolve", "af.publish"]
        self.write(self.entry(ops))
        loaded = self.load()
        registry = loaded.registry_entry()
        tool, = loaded.tools_list()
        self.assertEqual(registry["origin"], "factory")
        self.assertEqual(registry["ops"], ops)
        self.assertEqual(tool["name"], m1.TOOL)
        self.assertEqual(tool["inputSchema"]["properties"]["op"]["enum"], ops)
        self.assertEqual(loaded.cli_ops(), {"factory": ops})
        self.assertEqual(loaded.json_lane_ops(), {m1.TOOL: ops})
        validator = Draft202012Validator(tool["inputSchema"])
        wire = {"request_id": "r", "dry_run": True, "op": ops[0], "goal_id": "g",
                "inputs": [{"schema": "vp.artifact_ref/v1", "locator": "af://fixtures/input",
                            "sha256": "a" * 64, "bytes": 1, "media_type": "image/png",
                            "stage": "input", "copy": "none"}], "session_id": "s"}
        validator.validate(wire)
        for malformed in (
            {"request": wire}, dict(wire, op="af.generate"), dict(wire, inputs=[{"sha256": "x"}]),
            dict(wire, caller="operator"), dict(wire, seed="0"),
        ):
            with self.subTest(malformed=malformed), self.assertRaises(ValidationError):
                validator.validate(malformed)
        registry["ops"].clear()
        self.assertEqual(loaded.cli_ops(), {"factory": ops})

    def test_every_frozen_factory_op_has_a_dry_run_receipt(self):
        self.write(self.entry(self.catalog))
        loaded = self.load()
        raw_hash = sha256(self.path.read_bytes()).hexdigest()
        for op in self.catalog:
            with self.subTest(op=op):
                operation = request(op, caller="operator")
                receipt = loaded.preview(operation)
                self.assertEqual(receipt["schema"], m1.REPLY)
                self.assertEqual(receipt["effective_request_sha256"], digest(operation))
                self.assertEqual(receipt["evidence"][0]["sha256"], raw_hash)
                self.assertEqual(receipt["code"], "HOLD")
                self.assertEqual(receipt["result"], {})

    def test_offline_cli_emits_canonical_receipt(self):
        self.write(self.entry(["vqa3d.score"]))
        args = self.root / "request.json"
        operation = request("vqa3d.score")
        args.write_text(json.dumps(operation), encoding="utf-8")
        output = io.StringIO()
        loaded = self.load()
        with patch.object(m1, "load_manifest", return_value=loaded), redirect_stdout(output):
            rc = m1.main(["--manifest", str(self.path), "--json-arg", str(args), "--dry-run"])
        self.assertEqual(rc, 3)
        self.assertEqual(json.loads(output.getvalue()), loaded.preview(operation))

    def test_unknown_operation_and_non_preview_are_refused(self):
        entry = self.entry(["af.generate"])
        self.write(entry)
        loaded = self.load()
        with self.assertRaisesRegex(m1.ManifestError, "absent"):
            loaded.preview(request("af.repair"))
        with self.assertRaisesRegex(m1.ManifestError, "dry-run"):
            loaded.preview(dict(request("af.generate"), dry_run=False))
        entry.pop("op_table")
        entry["ops"].append("af.unknown")
        self.write(entry)
        with self.assertRaises(m1.ManifestError):
            self.load()

    def test_positive_receipt_and_binding_gate(self):
        catalog = self.bound()
        entry = self.entry(["af.generate"], catalog=catalog)
        entry["provider"] = provider(receipt=self.receipt)
        self.write(entry)
        with patch.object(m1, "_contracts", return_value=(self.args, self.control, self.levels, catalog)):
            loaded = self.load(self.pin)
            self.assertTrue(loaded.registry_entry()["supported"])
            reply = loaded.preview(request("af.generate"))
            self.assertEqual(reply["code"], "OK")
            self.assertEqual(reply["result"]["artifact_refs"], [])
            self.assertEqual(reply["result"]["receipt"], reply["evidence"][0])
        # The same declaration cannot supply or replace the actual frozen binding.
        with self.assertRaisesRegex(m1.ManifestError, "binding"):
            self.load(self.pin)

    def assert_unqualified_refused(self, loaded):
        entry = loaded.registry_entry()
        self.assertFalse(entry["supported"], "unqualified op became routable")
        self.assertFalse(entry["op_table"][0]["supported"])
        self.assertEqual(loaded.preview(request("af.generate"))["code"], "HOLD")

    def test_unverified_or_missing_qualification_loaded_unsupported(self):
        catalog = self.bound()
        with patch.object(m1, "_contracts", return_value=(self.args, self.control, self.levels, catalog)):
            for model, pins in (
                (provider(), (self.pin,)),
                (provider(receipt=self.receipt), ()),
                (provider(receipt=self.receipt), (("Other model", *self.pin[1:]),)),
                (provider(receipt=self.receipt), ((self.pin[0], "4" * 64, *self.pin[2:]),)),
                (None, (self.pin,)),
            ):
                entry = self.entry(["af.generate"], catalog=catalog)
                if model is not None:
                    entry["provider"] = model
                self.write(entry)
                with self.subTest(model=model, pins=pins):
                    loaded = self.load(*pins)
                    self.assert_unqualified_refused(loaded)
                    if model is not None:
                        qualification = loaded.registry_entry()["provider"]["model"]["qualification"]
                        self.assertEqual(qualification["state"], "unqualified")
            entry = self.entry(["af.generate"], catalog=catalog)
            entry["provider"] = provider(receipt=self.receipt)
            entry["provider"]["model"]["license"]["territory_ok"] = False
            self.write(entry)
            self.assert_unqualified_refused(self.load(self.pin))

    def test_hunyuan_refused_during_load(self):
        entry = self.entry(["af.generate"])
        for name in ("Hunyuan3D-2.1", "hunyuan3d21"):
            entry["provider"] = provider(name=name, receipt=self.receipt)
            self.write(entry)
            with self.subTest(name=name), self.assertRaisesRegex(m1.ManifestError, "excluded"):
                self.load(self.pin)

    def test_publish_is_operator_only_and_parked(self):
        entry = self.entry(["af.publish"])
        entry.pop("op_table")
        self.write(entry)
        loaded = self.load()
        self.assertTrue(loaded.registry_entry()["operator_only"])
        self.assertFalse(loaded.registry_entry()["supported"])
        self.assertEqual(loaded.preview(request("af.publish"))["code"], "OWNER")
        self.assertEqual(loaded.preview(request("af.publish", caller="operator"))["code"], "HOLD")
        entry = self.entry(["af.publish"])
        entry["op_table"][0]["operator_only"] = False
        self.write(entry)
        with self.assertRaises(m1.ManifestError):
            self.load()

    def test_manifest_cannot_lower_frozen_risk(self):
        entry = self.entry(["af.generate"])
        entry["op_table"][0]["risk_class"] = "leer"
        self.write(entry)
        with self.assertRaises(m1.ManifestError):
            self.load()

    def test_unqualified_routable_mutant_is_killed(self):
        catalog = self.bound()
        self.write(self.entry(["af.generate"], catalog=catalog))
        original = m1._row

        def bypass_qualification(op, declaration, frozen, qualified, levels):
            return original(op, declaration, frozen, True, levels)

        with patch.object(m1, "_contracts", return_value=(self.args, self.control, self.levels, catalog)):
            self.assert_unqualified_refused(self.load())
            with patch.object(m1, "_row", side_effect=bypass_qualification):
                with self.assertRaisesRegex(AssertionError, "unqualified op became routable"):
                    self.assert_unqualified_refused(self.load())

    def test_downward_risk_override_mutant_is_killed(self):
        stricter = self.bound(risk="enviar")
        entry = self.entry(["af.generate"], catalog=stricter)
        entry["op_table"][0]["risk_class"] = "preparar"
        self.write(entry)

        def assert_refusal():
            with self.assertRaises(m1.ManifestError):
                self.load()

        original = m1._row

        def trust_manifest_risk(op, declaration, frozen, qualified, levels):
            weakened = {**frozen, op: {**frozen[op], "risk_class": declaration["risk_class"]}}
            return original(op, declaration, weakened, qualified, levels)

        with patch.object(m1, "_contracts", return_value=(self.args, self.control, self.levels, stricter)):
            assert_refusal()
            with patch.object(m1, "_row", side_effect=trust_manifest_risk):
                with self.assertRaisesRegex(AssertionError, "ManifestError not raised"):
                    assert_refusal()

    def test_duplicate_entry_and_schema_pin_are_refused(self):
        entry = self.entry(["af.generate"])
        self.write(entry, entry)
        with self.assertRaisesRegex(m1.ManifestError, "registry entry"):
            self.load()
        entry["args_schema_sha256"] = "4" * 64
        self.write(entry)
        with self.assertRaisesRegex(m1.ManifestError, "digest mismatch"):
            self.load()

    def test_manifest_path_cannot_leave_factory_root(self):
        self.write(self.entry(["af.generate"]))
        other = self.root / "factory"
        other.mkdir()
        escape = other / "escape.json"
        escape.symlink_to(self.path)
        with self.assertRaisesRegex(m1.ManifestError, "under the asset-factory root"):
            m1.load_manifest(escape, asset_factory_root=other)
