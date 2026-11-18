"""Operator declarations and the pinned TRELLIS geometry boundary."""

import ast
import importlib.abc
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from asset_factory import core
from licence import check as licence
from tools.qualification import manifest

ROOT = Path(__file__).resolve().parents[1]
QUOTE = Path("quotes/operator_1118_decisions_0815.txt")
OPERATOR_CONFIG = ROOT / "config/factory.operator-1118.yaml"
PIPELINE = "$AF_MODELS_ROOT/TRELLIS.2-4B/pipeline.json"
PROVIDER = ROOT / "providers/trellis2"
RUNTIME = PROVIDER / "runtime.py"
ENTRYPOINT = PROVIDER / "qualification/adapter.py"
SANDBOX_RUNTIME = PROVIDER / "qualification/sandbox_runtime.py"
REFERENCE = "briaai/RMBG-2.0"
PIPELINE_SHA256 = "222c359ab1ed9bc6735a640a34f95d47f8681b9bc4aaa101bfb80274676253c6"


def pin(path):
    return {"path": str(path), "sha256": core.sha256_file(path)}


class RightsDecisions(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)

    def json_pin(self, name, data):
        path = self.temp / name
        core.atomic_write_json(path, data)
        return pin(path)

    def candidate(self, provider, weights):
        empty_object = {"type": "object", "additionalProperties": False}
        dependency = self.json_pin("runtime.json", {})
        interpreter = pin(Path(sys.executable).resolve())
        lock = self.json_pin("runtime-lock.json", {
            "schema": "af.qualification-python-runtime/v1",
            "interpreter_sha256": interpreter["sha256"],
            "files": [dependency],
            "import_roots": [],
        })
        return {
            "schema": "af.qualification-candidate/v1",
            "provider": provider,
            "code": {
                "revision": core.sha256_file(ENTRYPOINT),
                "entrypoint": pin(ENTRYPOINT),
                "files": [pin(RUNTIME), pin(SANDBOX_RUNTIME)],
            },
            "weights": weights,
            "conditioners": [],
            "dependencies": [],
            "runtime": {"interpreter": interpreter, "files": [dependency], "lock": lock, "cuda": False},
            "request": self.json_pin("request.json", {}),
            "input_schema": self.json_pin("input-schema.json", empty_object),
            "output_schema": self.json_pin("output-schema.json", empty_object),
            "outputs": ["result.json"],
        }

    def test_operator_config_declares_commercial_use_with_quote_provenance(self):
        if not OPERATOR_CONFIG.is_file():
            self.skipTest("private operator config is absent")
        quote = manifest.pin_path(f"$AF_CAMPAIGN_ROOT/{QUOTE}")
        policy = licence.load_policy(ROOT / "config/territory-groups.v1.json")
        config = licence.load_factory_config(OPERATOR_CONFIG)
        self.assertIs(config["commercial_use"], True)
        self.assertIn(config["jurisdiction"], policy["groups"]["EU"])
        self.assertIn(f"# operator_quote_sha256: {core.sha256_file(quote)}",
                      OPERATOR_CONFIG.read_text(encoding="utf-8").splitlines())
        self.assertIn(QUOTE.as_posix(), OPERATOR_CONFIG.read_text(encoding="utf-8"))
        incomplete = self.json_pin("no-commercial-use.json", {
            key: value for key, value in config.items() if key != "commercial_use"
        })
        self.assertNotIn("commercial_use", licence.load_factory_config(Path(incomplete["path"])))
        # The frozen factory decision records the declaration on a refusal too.
        excluded = core.load_json(ROOT / "providers/hunyuan3d21/provider.json", max_bytes=manifest.JSON_LIMIT)
        decision = licence.check_provider(config, excluded, operation="generate", policy=policy)
        self.assertEqual(decision["schema"], "af.licence-decision/v1")
        self.assertIs(decision["commercial_use"], True)
        self.assertEqual(decision["configuration_sha256"], core.sha256_json(config))
        self.assertIs(decision["allowed"], False)
        self.assertEqual(decision["reason_codes"], ["provider_blocked"])

    def test_commercial_declaration_does_not_complete_incomplete_local_manifests(self):
        for provider in ("unirig", "mocapanything_v2", "partcrafter", "triposg"):
            with self.subTest(provider=provider):
                path = ROOT / "providers" / provider / "qualification" / "candidate.json"
                candidate = core.load_json(path, max_bytes=manifest.JSON_LIMIT)
                self.assertIsNone(candidate["runtime"]["interpreter"])
                # The adapter can advance before INT re-pins this draft. The hygiene suite
                # separately insists that all shipped code pins match the checkout.
                entry = candidate["code"]["entrypoint"]
                stale = core.sha256_file(ROOT / entry["path"]) != entry["sha256"]
                rc, reason = (4, "pin_digest_mismatch") if stale else (2, "missing_or_invalid_file_pin")
                for reviewed in (candidate, candidate | {
                    "licence_metadata": candidate["licence_metadata"] | {"commercial_use": True},
                }):
                    with self.assertRaisesRegex(manifest.Refusal, f"^{reason}$") as caught:
                        manifest.check_candidate(reviewed, manifest.Pins())
                    self.assertEqual(caught.exception.rc, rc)

    def test_exact_pipeline_pin_is_not_an_allowlist_exception(self):
        local = manifest.pin_path(PIPELINE)
        data = json.loads(local.read_bytes())
        self.assertEqual(data["args"]["rembg_model"]["args"]["model_name"], REFERENCE)
        pipeline_pin = pin(local)
        self.assertEqual(pipeline_pin["sha256"], PIPELINE_SHA256)
        for provider in ("trellis2", "custom-local"):
            with self.subTest(provider=provider):
                candidate = self.candidate(provider, [pipeline_pin])
                _, entrypoint, _, _ = manifest.check_candidate(candidate, manifest.Pins())
                self.assertEqual(entrypoint, ENTRYPOINT)

    def test_changed_pipeline_still_requires_matching_digest(self):
        changed = self.temp / "pipeline.json"
        changed.write_bytes(manifest.pin_path(PIPELINE).read_bytes() + b"\n")
        candidate = self.candidate("trellis2", [pin(changed)])
        manifest.check_candidate(candidate, manifest.Pins())
        candidate["weights"][0]["sha256"] = PIPELINE_SHA256
        with self.assertRaisesRegex(manifest.Refusal, "^pin_digest_mismatch$"):
            manifest.check_candidate(candidate, manifest.Pins())

    def test_static_geometry_loader_bypasses_the_stock_remover(self):
        def function(path, name):
            return next(node for node in ast.parse(path.read_text()).body
                        if isinstance(node, ast.FunctionDef) and node.name == name)

        loader = function(RUNTIME, "load_pipeline")
        kwargs, = [node.value for node in loader.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "kwargs"
                           for target in node.targets)]
        self.assertIsInstance(kwargs, ast.Dict)
        pairs = {key.value: value for key, value in zip(kwargs.keys, kwargs.values)}
        self.assertIsNone(ast.literal_eval(pairs["rembg_model"]))
        returned, = [node.value for node in ast.walk(loader) if isinstance(node, ast.Return)]
        self.assertEqual(ast.unparse(returned), "Trellis2GeometryPipeline(**kwargs)")
        pretrained = [ast.unparse(node.func) for node in ast.walk(loader)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr == "from_pretrained"]
        self.assertEqual(pretrained, ["models.from_pretrained"])
        self.assertNotIn("rembg_model", ast.unparse(function(RUNTIME, "local_plan")))
        calls = {ast.unparse(node.func) for node in ast.walk(function(ENTRYPOINT, "main"))
                 if isinstance(node, ast.Call)}
        self.assertTrue({"runtime.execute", "sandbox_runtime.execute"} <= calls)
        for path in (RUNTIME, SANDBOX_RUNTIME):
            with self.subTest(path=path):
                run, = [node for node in ast.walk(function(path, "execute"))
                        if isinstance(node, ast.Call) and ast.unparse(node.func) == "pipeline.run"]
                preprocess, = [item.value for item in run.keywords if item.arg == "preprocess_image"]
                self.assertIs(ast.literal_eval(preprocess), False)

    def test_run_free_runtime_import_never_resolves_model_or_remover_modules(self):
        def model_module(name):
            parts = name.casefold().split(".")
            return (parts[0] in {"trellis2", "torch", "transformers", "huggingface_hub"}
                    or any(part.startswith(("rembg", "rmbg", "bria", "birefnet")) for part in parts))

        class RejectModelImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if model_module(fullname):
                    raise AssertionError(f"Unexpected model import: {fullname}")

        before = set(sys.modules)
        spec = importlib.util.spec_from_file_location("providers.trellis2._rights_import_probe", RUNTIME)
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "meta_path", [RejectModelImports(), *sys.meta_path]):
            spec.loader.exec_module(module)
        self.assertTrue(callable(module.load_pipeline))
        self.assertEqual(sorted(name for name in set(sys.modules) - before if model_module(name)), [])


if __name__ == "__main__":
    unittest.main()
