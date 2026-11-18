"""Offline controls for the motion adapter's foreground boundary."""

import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from asset_factory import core
from providers.mocapanything_v2 import adapter, offline
from providers.mocapanything_v2.bvh import Refusal
from tools.qualification.manifest import Pins, Refusal as PinRefusal


class MoCapClosureTests(unittest.TestCase):
    def test_changed_closure_file_is_stale(self):
        candidate_path = adapter.ROOT / "qualification" / "candidate.json"
        candidate = json.loads(candidate_path.read_bytes())
        descriptor = candidate["code"]["entrypoint"]
        entrypoint = Path(descriptor["path"])
        self.assertEqual(entrypoint.resolve(), Path(adapter.__file__).resolve())
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / entrypoint.name
            raw = entrypoint.read_bytes()
            copy.write_bytes(raw)
            pin = {**descriptor, "path": str(copy)}
            self.assertEqual(Pins().check(pin), copy.resolve())
            copy.write_bytes(raw + b"\n# pin drift\n")
            with self.assertRaises(PinRefusal) as error:
                Pins().check(pin)
            self.assertEqual((error.exception.rc, error.exception.code), (4, "pin_digest_mismatch"))

    def test_remover_segmentation_config_refuses_before_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe_dir = root / "build"
            recipe_dir.mkdir()
            for selection in ("rmbg", "briaai/RMBG-1.4"):
                with self.subTest(selection=selection):
                    recipe = {"segmentation": selection, "network": "deny"}
                    (recipe_dir / "recipe.json").write_text(core.canonical_json(recipe))
                    with patch.object(adapter, "ROOT", root), patch.object(
                            adapter, "recipe_preflight", side_effect=AssertionError("preflight called")):
                        proof = adapter.preflight()
                    self.assertEqual((proof["rc"], proof["verdict"], proof["reason_codes"]),
                                     (2, "ERROR", ["network_recipe_invalid"]))

    def test_locally_loaded_remover_name_does_not_block_the_pinned_foreground(self):
        name = "preprocess.briarmbg"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {name: ModuleType(name)}):
            root = self.model_root(tmp)
            with patch.object(offline, "load_birefnet", return_value=lambda image: image) as loader:
                self.assertEqual(adapter.load_segmentation(root)("frame"), "frame")
                loader.assert_called_once_with(root.resolve())
            with patch.object(adapter, "recipe_preflight", return_value={"rc": 0}) as preflight:
                self.assertEqual(adapter.preflight(), {"rc": 0})
                preflight.assert_called_once_with()

    def test_recipe_identity_and_inventory_refuse_before_birefnet(self):
        for mutation in ("repo", "revision", "missing_file", "duplicate_file", "changed_digest"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                model_root = self.model_root(tmp)
                recipe = adapter.load(adapter.ROOT / "build" / "recipe.json")
                model = recipe["models"]["birefnet"]
                if mutation == "repo":
                    model["repo"] = "briaai/RMBG-1.4"
                elif mutation == "revision":
                    model["revision"] = "a" * 40
                elif mutation == "missing_file":
                    model["files"].pop()
                elif mutation == "duplicate_file":
                    model["files"].append(model["files"][0])
                else:
                    model["files"][0]["sha256"] = "b" * 64
                (root / "build").mkdir()
                (root / "build" / "recipe.json").write_text(core.canonical_json(recipe))
                with patch.object(adapter, "ROOT", root), patch.object(
                        offline, "load_birefnet", return_value=lambda image: image) as loader:
                    with self.assertRaises(Refusal) as error:
                        adapter.load_segmentation(model_root)
                    loader.assert_not_called()
                self.assertEqual((error.exception.rc, error.exception.reason),
                                 (4, "birefnet_recipe_stale"))

    def test_missing_model_root_refuses_before_loading(self):
        with patch.object(offline, "load_birefnet") as loader:
            with self.assertRaises(Refusal) as error:
                adapter.load_segmentation(None)
            loader.assert_not_called()
        self.assertEqual((error.exception.rc, error.exception.reason), (3, "birefnet_root_missing"))

    def test_routes_to_pinned_birefnet_foreground(self):
        image, expected = object(), object()
        with tempfile.TemporaryDirectory() as tmp:
            root = self.model_root(tmp)

            def foreground(value):
                self.assertIs(value, image)
                return expected

            with patch.object(offline, "load_birefnet", return_value=foreground) as loader:
                self.assertIs(adapter.load_segmentation(root)(image), expected)
                loader.assert_called_once_with(root.resolve())

    def test_extra_weight_file_refuses_before_birefnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.model_root(tmp)
            (root / "pytorch_model.bin").touch()
            with patch.object(offline, "load_birefnet", side_effect=AssertionError("loader called")):
                with self.assertRaises(Refusal) as error:
                    adapter.load_segmentation(root)
        self.assertEqual((error.exception.rc, error.exception.reason), (4, "unlisted_model_file"))

    def test_missing_pinned_file_refuses_before_birefnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.model_root(tmp)
            (root / "model.safetensors").unlink()
            with patch.object(offline, "load_birefnet", return_value=lambda image: image) as loader:
                with self.assertRaises(Refusal) as error:
                    adapter.load_segmentation(root)
                loader.assert_not_called()
        self.assertEqual((error.exception.rc, error.exception.reason), (3, "local_weight_missing"))

    @staticmethod
    def model_root(tmp):
        root = Path(tmp) / "model"
        root.mkdir()
        recipe = adapter.load(adapter.ROOT / "build" / "recipe.json")
        for pin in recipe["models"]["birefnet"]["files"]:
            (root / pin["path"]).touch()
        return root
