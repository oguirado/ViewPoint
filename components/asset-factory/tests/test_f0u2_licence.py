"""F0-U2 rule pairs and executable mutants, usable under python3 -I."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# -I ignores PYTHONPATH. Trust exactly this file's component root for discovery.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from licence import check as checker

FIXTURES = ROOT / "tests/fixtures/f0u2"


class LicenceTests(unittest.TestCase):
    def setUp(self):
        self.factory = json.loads((FIXTURES / "factory.json").read_text())
        self.provider = json.loads((FIXTURES / "provider.json").read_text())
        self.policy = checker.load_policy(ROOT / "config/territory-groups.v1.json")
        self.hunyuan = json.loads((ROOT / "providers/hunyuan3d21/provider.json").read_text())

    def decision(self, provider=None, operation="generate"):
        return checker.check_provider(self.factory, self.provider if provider is None else provider,
                                      operation=operation, policy=self.policy)

    def outcome(self, action, rc, reason=None):
        try:
            result = action()
        except checker.core.ContractError as exc:
            result = exc.as_dict()
        self.assertIsInstance(result, dict)
        self.assertIn("rc", result, "a refusal must expose its exact stage code")
        self.assertEqual(result["rc"], rc, result)
        if reason:
            self.assertIn(reason, result["reason_codes"])
        return result

    def dependency(self, **changes):
        dep = {"name": "synthetic_nvdiffrast", "licence": "NVIDIA Source Code License-NC",
               "commercial": False, "territory_excluded": [], "gated": False,
               "used_by": ["uv_texture"]}
        dep.update(changes)
        self.provider["licence"]["deps"] = [dep]
        return dep

    def load_text(self, text, suffix=".json"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ("factory" + suffix)
            path.write_text(text, encoding="utf-8")
            return checker.load_factory_config(path)

    def main_result(self, *, provider=None, name=None):
        provider = self.provider if provider is None else provider
        name = provider["id"] if name is None else name
        factory = deepcopy(self.factory)
        factory["slots"]["generate"]["provider"] = name
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps(factory))
            providers = root / "providers"
            providers.mkdir()
            if name == provider["id"]:
                target = providers / name
                target.mkdir()
                (target / "provider.json").write_text(json.dumps(provider))
            stream = io.StringIO()
            with redirect_stdout(stream):
                rc = checker.main(["--config", str(config), "--providers-root", str(providers),
                                   "--operation", "generate"])
            return rc, json.loads(stream.getvalue()), stream.getvalue()

    def test_explicit_permissive_fixture_allowed(self):
        decision = self.outcome(self.decision, 0)
        self.assertTrue(decision["allowed"])
        self.assertTrue(decision["monetisable"])
        self.assertEqual(decision["verdict"], "PASS")
        del self.provider["licence"]
        self.outcome(self.decision, 2, "provider_invalid")

    def test_hunyuan_es_refused(self):
        result = self.outcome(lambda: self.decision(self.hunyuan), 5)
        self.assertEqual(result["reason_codes"], ["provider_blocked"])
        self.assertFalse(result["allowed"])
        self.assertFalse(result["monetisable"])
        self.assertIsNone(self.hunyuan["revision"])
        self.assertEqual(list((ROOT / "providers/hunyuan3d21").iterdir()),
                         [ROOT / "providers/hunyuan3d21/provider.json"])

    def test_eu_country_expansion(self):
        self.provider["territory_excluded"] = ["EU"]
        for country in self.policy["groups"]["EU"]:
            with self.subTest(country=country):
                self.factory["jurisdiction"] = country
                self.assertEqual(self.outcome(self.decision, 0)["jurisdiction"], country)
        self.factory["jurisdiction"] = "US"
        self.outcome(self.decision, 0)
        self.factory["jurisdiction"] = "GB"
        self.outcome(self.decision, 0)  # GB is not an EU member.

    def test_uk_kr_declarations_pass_through(self):
        self.provider["territory_excluded"] = ["UK", "KR"]
        for country in ("GB", "KR"):
            self.factory["jurisdiction"] = country
            self.assertEqual(self.outcome(self.decision, 0)["jurisdiction"], country)
        self.factory["jurisdiction"] = "ES"
        self.outcome(self.decision, 0)

    def test_direct_country_exclusion(self):
        self.provider["territory_excluded"] = ["ES"]
        self.outcome(self.decision, 0)
        self.factory["jurisdiction"] = "FR"
        self.outcome(self.decision, 0)

    def test_standby_never_becomes_runnable_by_jurisdiction(self):
        self.factory["jurisdiction"] = "US"
        result = self.outcome(lambda: self.decision(self.hunyuan), 5)
        self.assertEqual(result["reason_codes"], ["provider_blocked"])
        self.outcome(self.decision, 0)

    def test_commercial_nvdiffrast_usage_is_informational(self):
        self.factory["commercial_use"] = True
        dep = self.dependency(used_by=["uv_texture", "render"])
        for operation in ("uv_texture", "render"):
            result = self.outcome(lambda: self.decision(operation=operation), 0)
            self.assertEqual(result["evaluated_dependencies"], [dep["name"]])
        result = self.outcome(self.decision, 0)
        self.assertEqual(result["evaluated_dependencies"], [])

    def test_noncommercial_allowed_and_marked_c8(self):
        self.dependency()
        result = self.outcome(lambda: self.decision(operation="uv_texture"), 0)
        self.assertFalse(result["monetisable"])
        self.provider["licence"]["deps"] = []
        self.provider["commercial"] = False
        self.assertFalse(self.outcome(self.decision, 0)["monetisable"])
        self.factory["commercial_use"] = True
        self.outcome(self.decision, 0)

    def test_dependency_territory_and_unknown_rights(self):
        dep = self.dependency(commercial=True, territory_excluded=["EU"])
        self.outcome(lambda: self.decision(operation="uv_texture"), 0)
        dep["territory_excluded"] = []
        self.outcome(lambda: self.decision(operation="uv_texture"), 0)
        dep["commercial"] = None
        self.outcome(lambda: self.decision(operation="uv_texture"), 0)
        self.outcome(self.decision, 0)

    def test_runnable_unknown_rights_are_informational(self):
        for key in ("commercial", "gated"):
            with self.subTest(key=key):
                original = self.provider[key]
                self.provider[key] = None
                self.outcome(self.decision, 0)
                self.provider[key] = original
                self.outcome(self.decision, 0)

    def test_gated_flags_are_informational(self):
        self.outcome(self.decision, 0)
        self.provider["gated"] = True
        self.outcome(self.decision, 0)
        self.provider["gated"] = False
        dep = self.dependency(commercial=True, gated=True)
        self.outcome(lambda: self.decision(operation="uv_texture"), 0)
        self.outcome(self.decision, 0)  # Dependency does not apply here.
        dep["gated"] = False
        self.outcome(lambda: self.decision(operation="uv_texture"), 0)
        dep.update(gated=True, territory_excluded=["EU"])
        self.outcome(lambda: self.decision(operation="uv_texture"), 0)

    def test_dependency_cannot_be_exempted_or_renamed(self):
        self.factory["commercial_use"] = True
        dep = self.dependency()
        self.outcome(lambda: self.decision(operation="uv_texture"), 0)
        dep["used_by"] = ["export"]
        self.outcome(self.decision, 2, "provider_invalid")
        dep["used_by"] = ["uv_texture"]
        dep["exempt"] = True
        self.outcome(self.decision, 2, "provider_invalid")

    def test_missing_commercial_use_is_unset(self):
        self.outcome(self.decision, 0)
        del self.factory["commercial_use"]
        self.assertIsNone(self.outcome(self.decision, 0)["commercial_use"])

    def test_string_false_not_boolean(self):
        self.outcome(self.decision, 0)
        for value in ("false", 0, None):
            self.factory["commercial_use"] = value
            self.outcome(self.decision, 2, "config_invalid")

    def test_unknown_jurisdiction_is_a_declaration(self):
        self.outcome(self.decision, 0)
        for value in ("ZZ", "es", "EU", "UK", ""):
            self.factory["jurisdiction"] = value
            self.assertEqual(self.outcome(self.decision, 0)["jurisdiction"], value)

    def test_missing_licence_territory_revision_rejected(self):
        passing = deepcopy(self.provider)
        self.outcome(self.decision, 0)
        for key in ("licence", "territory_excluded", "revision"):
            self.provider = deepcopy(passing)
            del self.provider[key]
            self.outcome(self.decision, 2, "provider_invalid")
        for revision in (None, "main", "master", "latest", "HEAD", ""):
            self.provider = deepcopy(passing)
            self.provider["revision"] = revision
            self.outcome(self.decision, 2, "provider_invalid")

    def test_model_weight_pins_and_measurement_reference(self):
        self.provider["kind"] = "model"
        self.provider["weights"] = [{"repo": "synthetic/model", "revision": "synthetic-v1",
                                     "files": [{"path": "synthetic.bin", "sha256": "a" * 64}]}]
        self.provider["runtime"]["ram_peak_gb_measured"] = 2
        self.provider["runtime"]["ram_peak_measurement_ref"] = {"path": "peak.json", "sha256": "b" * 64}
        self.outcome(self.decision, 0)
        complete = deepcopy(self.provider)
        mutations = [lambda p: p.update(weights=[]),
                     lambda p: p["weights"][0].update(revision="main"),
                     lambda p: p["weights"][0].update(files=[]),
                     lambda p: p["weights"][0]["files"][0].update(sha256="short"),
                     lambda p: p["runtime"].update(ram_peak_measurement_ref=None),
                     lambda p: p["runtime"].update(ram_peak_gb_measured=True)]
        for mutation in mutations:
            self.provider = deepcopy(complete)
            mutation(self.provider)
            self.outcome(self.decision, 2, "provider_invalid")

    def test_closed_config_and_integer_only_memory(self):
        self.outcome(self.decision, 0)
        passing = deepcopy(self.factory)
        mutations = [lambda f: f.update(extra=True),
                     lambda f: f["slots"].update(export={"provider": "none"}),
                     lambda f: f["slots"].pop("rig"),
                     lambda f: f["memory"].update(max_resident_models=True),
                     lambda f: f["memory"].update(max_resident_models=2),
                     lambda f: f["memory"].update(ceiling_gb_default=0),
                     lambda f: f["memory"].update(min_free_gb_before_start=-1),
                     lambda f: f["slots"]["generate"].update(params={"nested": []}),
                     lambda f: f["gates"]["appearance"].update(target=1)]
        for mutation in mutations:
            self.factory = deepcopy(passing)
            mutation(self.factory)
            self.outcome(self.decision, 2, "config_invalid")

    def test_legal_config_parse_error_no_default(self):
        self.assertEqual(self.load_text(json.dumps(self.factory)), self.factory)
        duplicate = json.dumps(self.factory)[:-1] + ', "commercial_use": true}'
        self.outcome(lambda: self.load_text(duplicate), 2)
        for text in ("{", "[]", "null", '{"schema":"af.factory-config/v1", "commercial_use":false, "commercial_use":true}'):
            self.outcome(lambda: self.load_text(text), 2)
        for text in ("{\"value\":0.5}", "{\"value\":NaN}", "{\"value\":Infinity}"):
            self.outcome(lambda: self.load_text(text), 2, "non_integer_number")

    def test_no_environment_substitution_and_scalar_params(self):
        self.factory["models_path"] = "${DO_NOT_EXPAND}"
        self.factory["slots"]["generate"]["params"] = {"seed": 42, "flag": True, "mode": "test"}
        with patch.dict("os.environ", {"DO_NOT_EXPAND": "secret"}):
            self.assertEqual(self.load_text(json.dumps(self.factory))["models_path"], "${DO_NOT_EXPAND}")
        self.factory["slots"]["generate"]["params"]["fraction"] = 0.5
        self.outcome(lambda: self.load_text(json.dumps(self.factory)), 2)

    def test_yaml_unavailable_does_not_disable_json(self):
        with patch.object(checker.importlib, "import_module", side_effect=ImportError):
            self.assertEqual(self.load_text(json.dumps(self.factory), ".yaml"), self.factory)
            self.outcome(lambda: self.load_text("schema: af.factory-config/v1\n", ".yaml"), 3, "yaml_unavailable")

    def test_duplicate_yaml_key_refused(self):
        try:
            import yaml
        except ImportError:
            self.outcome(lambda: self.load_text("schema: af.factory-config/v1\n", ".yaml"), 3, "yaml_unavailable")
            return
        text = yaml.safe_dump(self.factory)
        self.assertEqual(self.load_text(text, ".yaml"), self.factory)
        for suffix in ("commercial_use: true\n", "slots: {}\n"):
            self.outcome(lambda: self.load_text(text + suffix, ".yaml"), 2)
        for invalid in ("x: &a [*a]\n", "x: !!python/object:dict {}\n", "x: [1.5]\n", "[unclosed"):
            self.outcome(lambda: self.load_text(invalid, ".yaml"), 2)
        self.outcome(lambda: self.load_text("x: " + "[" * 40 + "0" + "]" * 40, ".yaml"), 6)

    def test_example_and_policy_are_valid(self):
        text = (ROOT / "config/factory.example.yaml").read_text()
        try:
            import yaml
        except ImportError:
            self.outcome(lambda: checker.load_factory_config(ROOT / "config/factory.example.yaml"), 3)
        else:
            self.assertEqual(self.load_text(text, ".yaml"), self.factory)
        self.assertEqual(len(self.policy["groups"]["EU"]), 27)
        self.assertNotIn("GB", self.policy["groups"]["EU"])
        original = deepcopy(self.policy)
        for mutation in (lambda p: p["groups"]["EU"].remove("ES"),
                         lambda p: p["groups"]["UK"].append("ES"),
                         lambda p: p["countries"].append("ES"),
                         lambda p: p.update(extra=True)):
            self.policy = deepcopy(original)
            mutation(self.policy)
            self.outcome(self.decision, 2, "config_invalid")

    def test_decision_binds_policy_provider_config(self):
        before = self.decision()
        for name, value in (("configuration", self.factory), ("provider", self.provider), ("policy", self.policy)):
            expected = hashlib.sha256((json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()).hexdigest()
            self.assertEqual(before[name + "_sha256"], expected)
        reordered = dict(reversed(list(self.factory.items())))
        self.assertEqual(checker.check_provider(reordered, self.provider, operation="generate", policy=self.policy), before)
        self.factory["models_path"] = "other-models"
        self.assertNotEqual(self.decision()["configuration_sha256"], before["configuration_sha256"])
        self.provider["revision"] = "synthetic-v2"
        self.assertNotEqual(self.decision()["provider_sha256"], before["provider_sha256"])
        self.policy["countries"].append("IS")
        self.assertNotEqual(self.decision()["policy_sha256"], before["policy_sha256"])

    def test_configuration_checks_optional_and_all_selected(self):
        for slot in ("generate", "render"):
            self.factory["slots"][slot] = {"provider": "synthetic_tool", "optional": True}
        result = checker.check_configuration(self.factory, {"synthetic_tool": self.provider}, policy=self.policy)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(set(result["decisions"]), {"generate", "render"})
        self.provider["territory_excluded"] = ["EU"]
        result = checker.check_configuration(self.factory, {"synthetic_tool": self.provider}, policy=self.policy)
        self.assertEqual(result["rc"], 0)
        self.outcome(lambda: checker.check_configuration(self.factory, {}, policy=self.policy), 2)

    def test_empty_configuration_is_cannot_see(self):
        result = checker.check_configuration(self.factory, {}, policy=self.policy)
        self.assertEqual(result, {"rc": 3, "verdict": "CANNOT_SEE", "decisions": {}})
        self.factory["slots"]["generate"]["provider"] = "synthetic_tool"
        self.assertEqual(checker.check_configuration(self.factory, {"synthetic_tool": self.provider}, policy=self.policy)["rc"], 0)

    def test_hunyuan_no_launch(self):
        with patch("subprocess.Popen", side_effect=AssertionError("must not launch")), \
                patch.object(checker.core, "atomic_write_json", side_effect=AssertionError("must not publish")):
            self.outcome(lambda: self.decision(self.hunyuan), 5)
            self.outcome(self.decision, 0)

    def test_refusal_preserves_output_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "caller-receipt.json"
            sentinel.write_bytes(b"caller bytes: \x00\xff\n")
            before = sentinel.read_bytes()
            with patch.object(Path, "write_text", side_effect=AssertionError("read-only checker")), \
                    patch.object(Path, "write_bytes", side_effect=AssertionError("read-only checker")), \
                    patch.object(checker.core, "atomic_write_json", side_effect=AssertionError("no publication")):
                self.outcome(lambda: self.decision(self.hunyuan), 5)
                self.outcome(self.decision, 0)
            self.assertEqual(sentinel.read_bytes(), before)

    def test_canonical_decision_and_stage_codes(self):
        for provider, expected in ((self.provider, 0), (self.hunyuan, 5)):
            rc, result, text = self.main_result(provider=provider)
            self.assertEqual(rc, expected)
            self.assertEqual(result["rc"], rc)
            self.assertNotEqual(rc, 1)
            self.assertEqual(text, json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
            self.assertEqual(result["verdict"], checker.core.VERDICT[rc])
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(checker.main([]), 2)
        self.assertEqual(json.loads(stream.getvalue())["rc"], 2)

    def test_cli_module_runs_from_component_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.factory["slots"]["generate"]["provider"] = self.provider["id"]
            config = root / "factory.json"
            config.write_text(json.dumps(self.factory))
            target = root / self.provider["id"]
            target.mkdir()
            (target / "provider.json").write_text(json.dumps(self.provider))
            run = subprocess.run([sys.executable, "-B", "-m", "licence.check", "--config", str(config),
                                  "--providers-root", str(root), "--operation", "generate"],
                                 cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(json.loads(run.stdout)["allowed"])

    def test_path_escape_and_symlink_refused(self):
        rc, result, _ = self.main_result(name="../outside")
        self.assertEqual(rc, 2)
        self.assertEqual(result["reason_codes"], ["path_escape"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            providers = root / "providers"
            providers.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (providers / "escape").symlink_to(outside, target_is_directory=True)
            self.factory["slots"]["generate"]["provider"] = "escape"
            config = root / "config.json"
            config.write_text(json.dumps(self.factory))
            with redirect_stdout(io.StringIO()) as output:
                rc = checker.main(["--config", str(config), "--providers-root", str(providers), "--operation", "generate"])
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(output.getvalue())["reason_codes"], ["path_escape"])
        self.assertEqual(self.main_result()[0], 0)

    def test_provider_identity_mismatch_is_error(self):
        with patch.object(checker, "_json", side_effect=lambda text: {**json.loads(text), "id": "other"} if '"synthetic_tool"' in text and '"runnable"' in text else json.loads(text)):
            self.assertEqual(self.main_result()[0], 2)
        self.assertEqual(self.main_result()[0], 0)

    def test_budget_limits_fail_closed(self):
        text = json.dumps(self.factory)
        self.assertEqual(self.load_text(text), self.factory)
        with patch.object(checker, "MAX_INPUT_BYTES", len(text.encode()) - 1):
            self.outcome(lambda: self.load_text(text), 6, "budget_exceeded")
        with patch.object(checker, "MAX_NODES", 1):
            self.outcome(self.decision, 6, "budget_exceeded")
        nested = []
        for _ in range(checker.MAX_DEPTH + 1):
            nested = [nested]
        self.outcome(lambda: checker._budget_tree(nested), 6, "budget_exceeded")

    def test_invalid_unicode_and_integer_work_bound(self):
        self.assertEqual(self.load_text(json.dumps(self.factory)), self.factory)
        for text in ('{"\\ud800":0}', '{"value":"\\ud800"}'):
            self.outcome(lambda: self.load_text(text), 2)
        self.factory["memory"]["ceiling_gb_default"] = 1 << checker.MAX_INTEGER_BITS
        self.outcome(self.decision, 6, "budget_exceeded")

    def test_nonregular_input_refused_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.fifo"
            os.mkfifo(path)
            self.outcome(lambda: checker.load_factory_config(path), 2, "config_invalid")
        self.assertEqual(self.load_text(json.dumps(self.factory)), self.factory)

    def test_nul_provider_name_and_symlink_loop_are_errors(self):
        self.assertEqual(self.main_result()[0], 0)
        self.factory["slots"]["generate"]["provider"] = "bad\x00name"
        self.outcome(self.decision, 2, "config_invalid")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "loop").symlink_to("loop")
            config = root / "factory.json"
            self.factory["slots"]["generate"]["provider"] = "synthetic_tool"
            config.write_text(json.dumps(self.factory))
            with redirect_stdout(io.StringIO()) as output:
                rc = checker.main(["--config", str(config), "--providers-root", str(root / "loop"), "--operation", "generate"])
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(output.getvalue())["reason_codes"], ["path_escape"])


class MutationProofTests(unittest.TestCase):
    """Break a rule temporarily, rerun its unchanged test, require a red gate."""
    def assert_mutant_red(self, name, target, replacement):
        suite = unittest.TestSuite([LicenceTests(name)])
        with patch.object(checker, target, replacement):
            result = unittest.TestResult()
            suite.run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertGreater(len(result.failures), 0, (name, result.errors))
        self.assertFalse(result.wasSuccessful())

    def test_whole_gate_rejects_allow_all_mutant(self):
        original = checker.check_provider
        def allow_all(*args, **kwargs):
            result = original(*args, **kwargs)
            result.update(rc=0, verdict="PASS", allowed=True)
            return result
        self.assert_mutant_red("test_hunyuan_es_refused", "check_provider", allow_all)

    def test_standby_mutant_goes_red(self):
        original = checker.check_provider
        def ignore_standby(factory, provider, **kwargs):
            provider = deepcopy(provider)
            if not provider["runnable"]:
                provider.update(runnable=True, revision="synthetic-v1", kind="tool", commercial=True, gated=False)
                provider.pop("blocked_reason")
            return original(factory, provider, **kwargs)
        self.assert_mutant_red("test_standby_never_becomes_runnable_by_jurisdiction", "check_provider", ignore_standby)

    def test_dependency_mutant_goes_red(self):
        original = checker.check_provider
        def ignore_dependencies(factory, provider, **kwargs):
            provider = deepcopy(provider)
            provider["licence"]["deps"] = []
            return original(factory, provider, **kwargs)
        self.assert_mutant_red("test_commercial_nvdiffrast_usage_is_informational", "check_provider", ignore_dependencies)

    def test_noncommercial_marking_mutant_goes_red(self):
        original = checker._record
        def ignore_marking(*args, **kwargs):
            result = original(*args, **kwargs)
            result["monetisable"] = True
            return result
        self.assert_mutant_red("test_noncommercial_allowed_and_marked_c8", "_record", ignore_marking)

    def test_schema_bypass_mutant_goes_red(self):
        original = checker.check_provider
        def coerce_boolean(factory, provider, **kwargs):
            factory = deepcopy(factory)
            factory["commercial_use"] = bool(factory["commercial_use"])
            return original(factory, provider, **kwargs)
        self.assert_mutant_red("test_string_false_not_boolean", "check_provider", coerce_boolean)

    def test_provider_bypass_mutant_goes_red(self):
        original = checker.check_provider
        def default_missing_legal_fields(factory, provider, **kwargs):
            defaults = json.loads((FIXTURES / "provider.json").read_text())
            provider = {**defaults, **provider}
            if not checker._pinned(provider["revision"]):
                provider["revision"] = defaults["revision"]
            return original(factory, provider, **kwargs)
        self.assert_mutant_red("test_missing_licence_territory_revision_rejected", "check_provider", default_missing_legal_fields)

    def test_duplicate_key_mutant_goes_red(self):
        self.assert_mutant_red("test_legal_config_parse_error_no_default", "_pairs", dict)

    def test_hash_binding_mutant_goes_red(self):
        suite = unittest.TestSuite([LicenceTests("test_decision_binds_policy_provider_config")])
        with patch.object(checker.core, "sha256_json", return_value="0" * 64):
            result = unittest.TestResult()
            suite.run(result)
        self.assertGreater(len(result.failures), 0)

    def test_refusal_write_mutant_goes_red(self):
        original = checker.check_provider
        def write_on_refusal(*args, **kwargs):
            checker.core.atomic_write_json(Path("forbidden.json"), {})
            return original(*args, **kwargs)
        self.assert_mutant_red("test_refusal_preserves_output_bytes", "check_provider", write_on_refusal)

    def test_publication_and_launch_mutant_goes_red(self):
        original = checker.check_provider
        def launch_anyway(*args, **kwargs):
            subprocess.Popen(["must-not-run"])
            return original(*args, **kwargs)
        self.assert_mutant_red("test_hunyuan_no_launch", "check_provider", launch_anyway)

    def test_budget_bypass_mutant_goes_red(self):
        self.assert_mutant_red("test_budget_limits_fail_closed", "_budget_tree", lambda *args: None)

    def test_yaml_duplicate_mutant_goes_red(self):
        try:
            import yaml
        except ImportError:
            # The optional branch is unavailable; JSON duplication has its own mutant.
            self.assert_mutant_red("test_yaml_unavailable_does_not_disable_json", "_yaml", lambda text: {})
        else:
            self.assert_mutant_red("test_duplicate_yaml_key_refused", "_pairs", dict)

    def test_path_escape_mutant_goes_red(self):
        suite = unittest.TestSuite([LicenceTests("test_path_escape_and_symlink_refused")])
        with patch.object(Path, "is_relative_to", return_value=True):
            result = unittest.TestResult()
            suite.run(result)
        self.assertGreater(len(result.failures), 0)

    def test_configuration_omission_mutant_goes_red(self):
        self.assert_mutant_red("test_configuration_checks_optional_and_all_selected", "check_configuration",
                               lambda *args, **kwargs: {"rc": 0, "verdict": "PASS", "decisions": {}})

    def test_canonical_output_mutant_goes_red(self):
        def unsorted_output(obj):
            print(json.dumps(obj, indent=2))
            return obj["rc"]
        suite = unittest.TestSuite([LicenceTests("test_canonical_decision_and_stage_codes")])
        with patch.object(checker.core, "emit", unsorted_output):
            result = unittest.TestResult()
            suite.run(result)
        self.assertGreater(len(result.failures), 0)

if __name__ == "__main__":
    unittest.main()
