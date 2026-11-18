"""Adversarial F0-U2 regressions: real filesystem races and module processes."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from licence import check as checker


class RepairTests(unittest.TestCase):
    def setUp(self):
        fixtures = ROOT / "tests/fixtures/f0u2"
        self.factory = json.loads((fixtures / "factory.json").read_text())
        self.provider = json.loads((fixtures / "provider.json").read_text())
        self.policy = checker.load_policy(ROOT / "config/territory-groups.v1.json")

    def decision(self, factory=None, provider=None, policy=None):
        return checker.check_provider(
            self.factory if factory is None else factory,
            self.provider if provider is None else provider,
            operation="generate", policy=self.policy if policy is None else policy)

    def race(self, replace_directory):
        self.factory["commercial_use"] = True
        self.factory["slots"]["generate"]["provider"] = self.provider["id"]
        restricted = deepcopy(self.provider)
        restricted["commercial"] = False
        self.assertEqual(self.decision(provider=restricted)["rc"], 0)
        self.assertEqual(self.decision()["rc"], 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "factory.json"
            config.write_text(json.dumps(self.factory))
            providers = root / "providers"
            target = providers / self.provider["id"]
            target.mkdir(parents=True)
            manifest = target / "provider.json"
            manifest.write_text(json.dumps(restricted))
            outside = root / "outside"
            outside.mkdir()
            outside_manifest = outside / "provider.json"
            outside_manifest.write_text(json.dumps(self.provider))
            real_open = os.open
            substituted = False

            def substitute_then_open(path, flags, *args, **kwargs):
                nonlocal substituted
                # Only schedule the substitution; every read uses the real OS.
                name = Path(path).name
                trigger = name == "provider.json" or (
                    replace_directory and name == self.provider["id"])
                if not substituted and trigger:
                    if replace_directory:
                        target.rename(providers / "original")
                        target.symlink_to(outside, target_is_directory=True)
                    else:
                        manifest.unlink()
                        manifest.symlink_to(outside_manifest)
                    substituted = True
                return real_open(path, flags, *args, **kwargs)

            with patch.object(checker.os, "open", side_effect=substitute_then_open):
                with redirect_stdout(io.StringIO()) as output:
                    rc = checker.main(["--config", str(config), "--providers-root",
                                       str(providers), "--operation", "generate"])
            result = json.loads(output.getvalue())
            self.assertTrue(substituted)
            self.assertEqual(rc, 2, result)
            self.assertEqual(result["reason_codes"], ["path_escape"])
            self.assertNotIn("allowed", result)

    def test_manifest_symlink_swap_before_open_refused(self):
        self.race(False)

    def test_manifest_parent_symlink_swap_before_open_refused(self):
        self.race(True)

    def test_inside_root_symlink_remains_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "storage"
            target.mkdir()
            (target / "provider.json").write_text(json.dumps(self.provider))
            (root / self.provider["id"]).symlink_to(target, target_is_directory=True)
            self.factory["slots"]["generate"]["provider"] = self.provider["id"]
            config = root / "factory.json"
            config.write_text(json.dumps(self.factory))
            with redirect_stdout(io.StringIO()) as output:
                rc = checker.main(["--config", str(config), "--providers-root", str(root),
                                   "--operation", "generate"])
            self.assertEqual(rc, 0, output.getvalue())
            self.assertTrue(json.loads(output.getvalue())["allowed"])

    def test_missing_or_corrupt_packaged_schemas_canonical_rc2(self):
        packaged = ("config/factory.schema.json", "licence/decision.schema.json")
        for schema in packaged:
            for damage in ("missing", "malformed", "nonobject"):
                with self.subTest(schema=schema, damage=damage):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        for relative in (*packaged, "licence/check.py", "asset_factory/__init__.py",
                                         "asset_factory/core.py", "asset_factory/contracts.py",
                                         "slots/provider.schema.json",
                                         "config/territory-groups.v1.json"):
                            dest = root / relative
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(ROOT / relative, dest)
                        damaged = root / schema
                        if damage == "missing":
                            damaged.unlink()
                        else:
                            damaged.write_text("{" if damage == "malformed" else "[]")
                        config = root / "factory.json"
                        config.write_text(json.dumps(self.factory))
                        run = subprocess.run(
                            [sys.executable, "-B", "-m", "licence.check", "--config", str(config),
                             "--providers-root", str(root), "--operation", "generate"],
                            cwd=root, capture_output=True, text=True, timeout=10)
                        self.assertEqual(run.returncode, 2, (run.stdout, run.stderr))
                        result = json.loads(run.stdout)
                        self.assertEqual(result["rc"], 2)
                        self.assertEqual(result["reason_codes"], ["config_invalid"])
                        self.assertEqual(run.stdout, checker.core.canonical_json(result))
                        self.assertEqual(run.stderr, "")

    def test_library_surrogates_raise_original_contract_error(self):
        for source, key in (("factory", "models_path"), ("provider", "id"), ("policy", "schema")):
            with self.subTest(source=source):
                data = {"factory": deepcopy(self.factory), "provider": deepcopy(self.provider),
                        "policy": deepcopy(self.policy)}
                data[source][key] = "\ud800"
                with self.assertRaises(checker.core.ContractError) as caught:
                    self.decision(**data)
                self.assertEqual(caught.exception.rc, 2)
                self.assertEqual(caught.exception.code, "config_invalid")
        self.assertEqual(self.decision()["rc"], 0)

    def test_decision_schema_rejects_contradictory_states(self):
        schema = json.loads((ROOT / "licence/decision.schema.json").read_text())
        schema = {k: v for k, v in schema.items() if k not in ("$id", "$schema")}
        validators = [("u2", lambda value: checker._shape(value, schema, "config_invalid"))]
        # Exercise the real U1 validator automatically when the sibling lands.
        if checker.core.__name__ == "asset_factory.core":
            validators.append(("u1", lambda value: checker.core.validate(value, schema)))
        valid = self.decision()
        for rc in (0, 2, 3, 5, 6):
            for verdict in ("PASS", "ERROR", "CANNOT_SEE", "FAIL"):
                for allowed in (False, True):
                    for monetisable in (False, True):
                        decision = dict(valid, rc=rc, verdict=verdict, allowed=allowed,
                                        monetisable=monetisable)
                        consistent = (verdict == checker.core.VERDICT[rc] and allowed == (rc == 0)
                                      and (rc == 0 or not monetisable))
                        for name, validate in validators:
                            with self.subTest(validator=name, rc=rc, verdict=verdict,
                                              allowed=allowed, monetisable=monetisable):
                                if consistent:
                                    validate(decision)
                                else:
                                    with self.assertRaises(checker.core.ContractError):
                                        validate(decision)


if __name__ == "__main__":
    unittest.main()
