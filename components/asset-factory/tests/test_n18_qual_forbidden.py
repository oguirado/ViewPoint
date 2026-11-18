import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.qualification import manifest


ROOT = Path(__file__).resolve().parents[1]
PIN_GROUPS = (
    ("request",),
    ("input_schema",),
    ("output_schema",),
    ("rights",),
    ("code", "entrypoint"),
    ("code", "files", 0),
    ("runtime", "interpreter"),
    ("runtime", "lock"),
    ("runtime", "files", 0),
    ("weights", 0),
    ("conditioners", 0, "files", 0),
    ("dependencies", 0, "files", 0),
    ("dependencies", 0, "licence"),
)


class ForbiddenComponentsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        schema = {"type": "object"}
        neutral = self.write_pin("component.bin", b"synthetic component")
        self.candidate = {
            "schema": manifest.CANDIDATE_SCHEMA,
            "provider": "qualification-fixture",
            "code": {"revision": neutral["sha256"], "entrypoint": neutral, "files": [neutral]},
            "runtime": {
                "interpreter": neutral,
                "files": [neutral],
                "lock": self.write_json("runtime.json", {
                    "schema": manifest.RUNTIME_SCHEMA,
                    "interpreter_sha256": neutral["sha256"],
                    "files": [neutral],
                    "import_roots": [],
                }),
                "cuda": False,
            },
            "weights": [self.write_pin("weights.bin", b"synthetic weights")],
            "conditioners": [{"name": "fixture-encoder", "files": [neutral]}],
            "dependencies": [{"name": "fixture-library", "files": [neutral], "licence": neutral}],
            "request": self.write_json("request.json", {}),
            "input_schema": self.write_json("input.json", schema),
            "output_schema": self.write_json("output.json", schema),
            "outputs": ["result.json"],
        }
        self.bind_rights(self.candidate)

    def write_pin(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return {"path": str(path), "sha256": core.sha256_file(path)}

    def write_json(self, name, value):
        return self.write_pin(name, core.canonical_json(value).encode())

    def bind_rights(self, candidate, verdict="PASS"):
        subject = {key: value for key, value in candidate.items() if key != "rights"}
        candidate["rights"] = self.write_json("rights.json", {
            "schema": manifest.RIGHTS_SCHEMA,
            "provider": subject["provider"],
            "jurisdiction": "EU",
            "commercial_use": True,
            "verdict": verdict,
            "subject_sha256": core.sha256_json(subject),
        })

    def test_neutral_fixture_reaches_success(self):
        manifest.check_candidate(self.candidate, manifest.Pins())

    def test_declared_path_in_each_pin_group(self):
        for group in PIN_GROUPS:
            with self.subTest(group=group):
                candidate = copy.deepcopy(self.candidate)
                if group == ("rights",):
                    self.bind_rights(candidate)
                parent = candidate
                for key in group[:-1]:
                    parent = parent[key]
                original = parent[group[-1]]
                alias = self.root / "RmBg.bin"
                alias.symlink_to(original["path"])
                parent[group[-1]] = original | {"path": str(alias)}
                if group == ("runtime", "files", 0):
                    candidate["runtime"]["lock"] = self.write_json("runtime-case.json", {
                        "schema": manifest.RUNTIME_SCHEMA,
                        "interpreter_sha256": candidate["runtime"]["interpreter"]["sha256"],
                        "files": candidate["runtime"]["files"],
                        "import_roots": [],
                    })
                if group != ("rights",):
                    self.bind_rights(candidate)
                try:
                    manifest.check_candidate(candidate, manifest.Pins())
                finally:
                    alias.unlink()

    def test_model_names_with_neutral_pins(self):
        for group in ("dependencies", "conditioners"):
            with self.subTest(group=group):
                candidate = copy.deepcopy(self.candidate)
                candidate[group][0]["name"] = "briaai/RMBG-1.4"
                self.bind_rights(candidate, verdict="CANNOT_SEE")
                manifest.check_candidate(candidate, manifest.Pins())

    def test_innocent_names_as_components_and_paths(self):
        for name in (
            "normbg", "normbg-adapter", "NORMBG", "brian", "brian-utils", "cambrian",
            "abria", "Umbria-tools", "Calabria/", "fabrian", "umbrella", "library",
            "xBriAx", "farm-bg", "rm-bgkeeper", "x-r_m_b_garden", "norm-bg",
        ):
            with self.subTest(name=name):
                directory = self.root / name
                directory.mkdir(exist_ok=True)
                candidate = copy.deepcopy(self.candidate)
                candidate["weights"] = [self.write_pin(f"{name.rstrip('/')}/w.bin", b"weights")]
                candidate["dependencies"][0]["name"] = name
                self.bind_rights(candidate)
                manifest.check_candidate(candidate, manifest.Pins())

    def test_fixture_under_normbg_tmpdir(self):
        evidence = self.root / "out" / "normbg"
        evidence.mkdir(parents=True)
        with patch.dict(os.environ, {"TMPDIR": str(evidence)}), patch.object(tempfile, "tempdir", None):
            fixture = ForbiddenComponentsTests("test_neutral_fixture_reaches_success")
            try:
                fixture.setUp()
                self.assertEqual(fixture.root.parent, evidence)
                fixture.test_neutral_fixture_reaches_success()
            finally:
                fixture.doCleanups()

    def test_names_accept_versions_separators_and_lookalikes(self):
        for name in (
            "rmbg", "bria", "briaai", "briarmbg", "rmbg14", "rmbg-1.4",
            "briaai/RMBG-1.4", "BRIA", "bria-fork", "rm-bg", "r_m_b_g",
            "RM BG", "b_r_i_a_a_i", "ＲＭＢＧ", "BRІA", "BRΙA",
            "RМВG", "RΜΒG", "BRIА", "BRIΑ", "ГMBԌ",
        ):
            with self.subTest(name=name):
                candidate = copy.deepcopy(self.candidate)
                candidate["dependencies"][0]["name"] = name
                self.bind_rights(candidate)
                manifest.check_candidate(candidate, manifest.Pins())

    def test_model_names_in_pinned_json_values(self):
        for role, filename in (
            ("request", "request-bad.json"),
            ("request", "extensionless-request"),
            ("input_schema", "input-bad.json"),
            ("output_schema", "output-bad.json"),
        ):
            with self.subTest(role=role, filename=filename):
                candidate = copy.deepcopy(self.candidate)
                value = {"rembg_model": "briaai/RMBG-1.4"}
                if role != "request":
                    value = {
                        "type": "object", "properties": {
                            "rembg_model": {"type": "string", "enum": ["briaai/RMBG-1.4"]},
                        },
                    }
                candidate[role] = self.write_json(filename, value)
                self.bind_rights(candidate)
                manifest.check_candidate(candidate, manifest.Pins())

    def test_pinned_code_configuration_is_not_licence_screened(self):
        for filename, content in (
            ("config.json", br'{"models":[{"model":"\u0062riaai/RMBG-1.4"}]}'),
            ("duplicate.json", b'{"model":"rm-bg","model":"normbg"}'),
            ("config.yml", br'models: [{model: "\u0062riaai/RMBG-1.4"}]'),
            ("config.yaml", b"models:\n  - model: r_m_b_g\n"),
            ("config.txt", "model: ＲＭＢＧ\n".encode()),
        ):
            with self.subTest(filename=filename):
                candidate = copy.deepcopy(self.candidate)
                candidate["code"]["files"].append(self.write_pin(filename, content))
                self.bind_rights(candidate)
                manifest.check_candidate(candidate, manifest.Pins())

    def test_structured_config_keys_and_innocent_values_are_allowed(self):
        for filename, content in (
            ("config.json", b'{"bria": ["normbg", "brian-utils"]}'),
            ("config.yaml", b"bria: [normbg, brian-utils]\n"),
            ("config.txt", b"model: normbg-adapter\n"),
        ):
            with self.subTest(filename=filename):
                candidate = copy.deepcopy(self.candidate)
                candidate["code"]["files"].append(self.write_pin(filename, content))
                self.bind_rights(candidate)
                manifest.check_candidate(candidate, manifest.Pins())

    def test_modified_config_refuses_as_pin_mismatch(self):
        pin = self.write_pin("config.txt", b"normbg")
        self.candidate["code"]["files"].append(pin)
        self.bind_rights(self.candidate)
        Path(pin["path"]).write_bytes(b"rmbg")
        with self.assertRaises(manifest.Refusal) as error:
            manifest.check_candidate(self.candidate, manifest.Pins())
        self.assertEqual((error.exception.rc, error.exception.code), (4, "pin_digest_mismatch"))

    def test_malformed_request_keeps_schema_error(self):
        self.candidate["request"] = self.write_pin("malformed.json", b"{")
        self.bind_rights(self.candidate)
        with self.assertRaises((manifest.Refusal, core.ContractError)) as error:
            manifest.check_candidate(self.candidate, manifest.Pins())
        self.assertEqual((error.exception.rc, error.exception.code), (2, "schema_mismatch"))

    def test_pinned_text_at_and_above_former_scan_cap(self):
        forbidden = b"rm-bg\n"
        for size in (manifest.JSON_LIMIT, manifest.JSON_LIMIT + 1):
            with self.subTest(size=size):
                candidate = copy.deepcopy(self.candidate)
                data = forbidden + b" " * (size - len(forbidden))
                candidate["code"]["files"].append(self.write_pin("config.txt", data))
                self.bind_rights(candidate)
                manifest.check_candidate(candidate, manifest.Pins())

    def test_neutral_symlink_resolves_to_bria_directory(self):
        (self.root / "BrIa").mkdir()
        target = self.write_pin("BrIa/weights.bin", b"synthetic checkpoint")
        intermediate = self.root / "intermediate.bin"
        intermediate.symlink_to(target["path"])
        alias = self.root / "neutral.bin"
        alias.symlink_to(intermediate)
        self.candidate["weights"] = [target | {"path": str(alias)}]
        self.bind_rights(self.candidate)
        manifest.check_candidate(self.candidate, manifest.Pins())

    def test_digest_checks_preserve_identity_after_denylist_removal(self):
        pin = self.candidate["weights"][0]
        with patch.object(manifest, "RMBG_WEIGHT_DIGESTS", {pin["sha256"]}, create=True):
            manifest.check_candidate(self.candidate, manifest.Pins())
        self.candidate["weights"] = [pin | {"sha256": "0" * 64}]
        self.bind_rights(self.candidate)
        with self.assertRaisesRegex(manifest.Refusal, "^pin_digest_mismatch$"):
            manifest.check_candidate(self.candidate, manifest.Pins())

    def test_current_candidates_carry_metadata_without_a_rights_pin(self):
        for provider in ("partcrafter", "triposg"):
            with self.subTest(provider=provider):
                path = ROOT / "providers" / provider / "qualification" / "candidate.json"
                candidate = core.load_json(path, max_bytes=manifest.JSON_LIMIT)
                self.assertNotIn("rights", candidate)
                self.assertEqual(candidate["licence_metadata"],
                                 {"jurisdiction": "EU", "commercial_use": None})
                with self.assertRaisesRegex(manifest.Refusal, "^missing_or_invalid_file_pin$") as caught:
                    manifest.check_candidate(candidate, manifest.Pins())
                self.assertEqual(caught.exception.rc, 2)
