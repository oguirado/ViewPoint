"""Publication hygiene across tracked and new factory text, including hidden files."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from asset_factory import core
from providers.trellis2 import admission
from tools.qualification.manifest import Pins, Refusal, pin_path
from tools.render.evaluate import NoiseMetric


ROOT = Path(__file__).resolve().parents[1]
OPERATOR_HOME = b"/home/" + b"admin"
EXCLUDED_AT_ROOT = {"build", ".cache"}
EXCLUDED_EVERYWHERE = {".git", "__pycache__"}
OPERATOR_CONFIG = Path("config/factory.operator-1118.yaml")


def home_path_hits(root):
    hits = []
    for directory, subdirs, files in os.walk(root):
        directory = Path(directory)
        excluded = EXCLUDED_EVERYWHERE
        if directory == root:
            excluded = excluded | EXCLUDED_AT_ROOT
        subdirs[:] = [name for name in subdirs if name not in excluded]
        for filename in files:
            path = directory / filename
            name = path.relative_to(root)
            if (name == OPERATOR_CONFIG or filename in {"auth.json", ".netrc", ".npmrc"}
                    or filename.startswith(".env")
                    or any(term in filename.casefold() for term in ("credential", "secret"))
                    or path.is_symlink()):
                continue
            raw = path.read_bytes()
            if b"\0" not in raw and OPERATOR_HOME in raw:
                hits.append(name.as_posix())
    return sorted(hits)


class HomePathHygieneTests(unittest.TestCase):
    def test_publishable_text_contains_no_operator_home(self):
        self.assertEqual(home_path_hits(ROOT), [])

    def test_reintroduced_home_path_in_nested_build_and_cache_is_detected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                "providers/trellis2/.cache/metadata.json",
                "providers/trellis2/build/measurements/gb10-512.json",
            ]
            for name in paths:
                path = root / name
                path.parent.mkdir(parents=True)
                path.write_bytes(OPERATOR_HOME + b"/synthetic\n")
            self.assertEqual(home_path_hits(root), paths)

    def test_local_and_metadata_paths_are_excluded(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (
                Path("build/output.txt"),
                Path(".cache/output.txt"),
                Path(".git/output.txt"),
                Path("__pycache__/output.txt"),
                Path("providers/trellis2/.git/output.txt"),
                Path("providers/trellis2/__pycache__/output.txt"),
                OPERATOR_CONFIG,
            ):
                path = root / name
                path.parent.mkdir(parents=True)
                path.write_bytes(OPERATOR_HOME + b"/synthetic\n")
            self.assertEqual(home_path_hits(root), [])

    def test_reintroduced_absolute_candidate_path_is_detected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "providers/partcrafter/qualification/candidate.json"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b'{"path":"$AF_MODELS_ROOT/model"}\n')
            self.assertEqual(home_path_hits(root), [])
            candidate.write_bytes(b'{"path":"' + OPERATOR_HOME + b'/model"}\n')
            self.assertEqual(home_path_hits(root), [candidate.relative_to(root).as_posix()])


class ReissuedPinTests(unittest.TestCase):
    def test_component_pin_is_independent_of_the_working_directory(self):
        relative = "providers/trellis2/provider.json"
        expected = ROOT / relative
        descriptor = {"path": relative, "sha256": core.sha256_file(expected)}
        with TemporaryDirectory() as temporary:
            previous = Path.cwd()
            try:
                os.chdir(temporary)
                self.assertEqual(Pins().check(descriptor), expected)
            finally:
                os.chdir(previous)

    def test_external_pin_requires_an_absolute_configured_root(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "model.json"
            artifact.write_text("{}")
            descriptor = {"path": "$AF_MODELS_ROOT/model.json",
                          "sha256": core.sha256_file(artifact)}
            with patch.dict(os.environ, {"AF_MODELS_ROOT": str(root)}):
                self.assertEqual(Pins().check(descriptor), artifact)
            for unavailable in ("", "."):
                with patch.dict(os.environ, {"AF_MODELS_ROOT": unavailable}):
                    with self.assertRaisesRegex(Refusal, "^pin_unavailable$"):
                        pin_path(descriptor["path"])

    def test_malformed_or_unknown_pin_root_is_refused(self):
        with patch.dict(os.environ, {"AF_MODELS_ROOT": str(ROOT)}):
            for name in ("$UNDECLARED_ROOT/file", "$AF_MODELS_ROOT", "$AF_MODELS_ROOT/",
                         "$AF_MODELS_ROOT//file"):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(Refusal, "^missing_or_invalid_file_pin$"):
                        pin_path(name)

    def test_pin_rejects_parent_traversal(self):
        with self.assertRaisesRegex(Refusal, "^missing_or_invalid_file_pin$"):
            pin_path("providers/../README.md")

    def test_root_relative_pin_rejects_an_escaping_symlink(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "models"
            root.mkdir()
            outside = base / "outside.json"
            outside.write_text("{}")
            (root / "alias.json").symlink_to(outside)
            with patch.dict(os.environ, {"AF_MODELS_ROOT": str(root)}):
                with self.assertRaisesRegex(Refusal, "^missing_or_invalid_file_pin$"):
                    pin_path("$AF_MODELS_ROOT/alias.json")

    def test_native_approval_and_lease_share_an_explicit_campaign_root(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {"AF_CAMPAIGN_ROOT": str(root)}):
                self.assertEqual(admission.approval_path(), root / admission.APPROVAL_RELATIVE)
                self.assertEqual(admission.campaign_path(admission.LEASE_RELATIVE),
                                 root / admission.LEASE_RELATIVE)
            for unavailable in ("", "."):
                with patch.dict(os.environ, {"AF_CAMPAIGN_ROOT": unavailable}):
                    with self.assertRaisesRegex(admission.Refusal, "^admission_evidence_missing$"):
                        admission.approval_path()

    def test_each_candidate_rebinds_its_product_files(self):
        candidates = list(ROOT.glob("providers/*/qualification/candidate.json"))
        self.assertTrue(candidates)
        for candidate_path in candidates:
            with self.subTest(candidate=candidate_path.parent.parent.name):
                candidate = core.load_json(candidate_path, max_bytes=1 << 20)
                code = candidate["code"]
                self.assertEqual(code["revision"], core.sha256_json(code["files"]))
                self.assertNotIn("rights", candidate)
                runtime = candidate["runtime"]
                pins = [
                    code["entrypoint"], *code["files"], *candidate["weights"], *runtime["files"],
                    runtime["interpreter"], runtime["lock"], candidate["request"],
                    candidate["input_schema"], candidate["output_schema"],
                ]
                for component in [*candidate["conditioners"], *candidate["dependencies"]]:
                    pins.extend(component["files"])
                    pins.append(component.get("licence"))
                for pin in pins:
                    if pin is not None and not pin["path"].startswith(("$", "/")):
                        self.assertEqual(core.sha256_file(pin_path(pin["path"])), pin["sha256"], pin)

    def test_latest_birefnet_summary_matches_sources_and_code_revision(self):
        provider = ROOT / "providers/birefnet"
        summary_path = provider / "qualification/candidate-pins.json"
        summary = core.load_json(summary_path, max_bytes=1 << 20)
        code = summary["code"]
        for relative, digest in summary["sources"].items():
            self.assertEqual(core.sha256_file(pin_path(relative)), digest, relative)
        # The code identity covers the component and upstream loader files, not the model checkpoint.
        model_sources = set(item["path"] for item in summary["model_files"]) - {"model.safetensors"}
        pinned_files = [
            pin for pin in code["files"]
            if pin["path"] in summary["sources"] or (
                pin["path"].startswith("$AF_MODELS_ROOT/BiRefNet/")
                and Path(pin["path"]).name in model_sources
            )
        ]
        self.assertEqual(core.sha256_json([code["entrypoint"], *pinned_files]), code["revision"])
        self.assertEqual(code["revision"], summary["code_revision"])

    def test_render_noise_metric_uses_reissued_film_profile_pins(self):
        identity = NoiseMetric(ROOT.parent / "blender-visual-qa").identity
        self.assertEqual(identity["profile_file_sha256"],
                         "4b9e5778ae3781c84c01fe184300037570e7c909d593df7ee66d2f07515be9ed")
        self.assertEqual(identity["profile_sha256"],
                         "94c7c8848600d3101a824c18a84465ac7cc43794b6340ea07d71ebf3fbccfe22")
