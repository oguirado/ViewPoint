# SPDX-License-Identifier: AGPL-3.0-only
"""Check the consumer validator against the same examples without modifying its registry."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import tempfile
import warnings

from jsonschema import ValidationError

from contracts.tests.test_contracts import ContractCase, ROOT, SCHEMAS, read


class ConsumerTests(ContractCase):
    def test_vp_direct_agreement(self):
        spec = importlib.util.spec_from_file_location("vp_direct_validation", SCHEMAS / "validation.py")
        consumer = importlib.util.module_from_spec(spec)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            spec.loader.exec_module(consumer)
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            for source in (*ROOT.glob("*.schema.json"), SCHEMAS / "vp.artifact_ref_v1.schema.json"):
                shutil.copyfile(source, stage / source.name)
            consumer.SCHEMA_DIR = stage
            for path in sorted((ROOT / "tests" / "examples").glob("*.json")):
                for case, instance in read(path).items():
                    with self.subTest(contract=path.stem, case=case):
                        try:
                            consumer.validate_contract(path.stem + "/v1", instance)
                            accepted = True
                        except ValidationError:
                            accepted = False
                        self.assertEqual(accepted, case != "invalid")
                        self.assertEqual(accepted, self.validator(path.stem).is_valid(instance))
            for name, expected in read(ROOT / "PINS.json")["files"].items():
                with self.subTest(file=name):
                    self.assertEqual(hashlib.sha256(consumer.canonical_bytes(read(ROOT / name))).hexdigest(), expected)
            for row in read(ROOT / "vp_rea_call.op_table.json")["op_table"].values():
                for field in ("args_schema", "reply_schema"):
                    with self.subTest(resource=row[field]):
                        resource = consumer._store()["urn:" + row[field].replace("/", ":")]
                        actual = hashlib.sha256(consumer.canonical_bytes(resource)).hexdigest()
                        self.assertEqual(actual, row[field + "_sha256"])
