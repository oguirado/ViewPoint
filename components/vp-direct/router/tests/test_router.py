from copy import deepcopy
from hashlib import sha256
import importlib.util
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import ValidationError


MODULE = Path(__file__).resolve().parents[1] / "router.py"
SPEC = importlib.util.spec_from_file_location("vp_router", MODULE)
router = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = router
SPEC.loader.exec_module(router)


class RouterTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "evidence.txt"
        self.source.write_text("Verified capability source.\n", encoding="utf-8")
        self.pin = {"path": self.source.name, "line_start": 1, "line_end": 1,
                    "sha256": sha256(self.source.read_bytes()).hexdigest()}
        self.golden = self.root / "golden.json"
        self.entries = [
            self.entry("vp_window_summon", ["summon"], ["window"]),
            self.entry("vp_window_dismiss", ["summon", "dismiss"], ["window", "panel", "desk"]),
            self.entry("vp_show", ["display"], ["panel"]),
            self.entry("vp_hide", ["display"], ["panel", "desk"]),
        ]
        self.cases = [
            {"id": "summon", "request": "bring the window to my desktop",
             "expected_verdict": "MATCH", "expected_tool": "vp_window_summon",
             "expected_args": {"session_id": "s-fixture"}},
            {"id": "dismiss", "request": "dismiss the panel at my desk",
             "expected_verdict": "MATCH", "expected_tool": "vp_window_dismiss",
             "expected_args": {"session_id": "s-fixture"}},
            {"id": "ood-near", "request": "display panel", "expected_verdict": "ABSTAIN",
             "expected_reason": "absent_capability", "expected_tool": None},
            {"id": "ood-far", "request": "book a flight to the moon",
             "expected_verdict": "ABSTAIN", "expected_reason": "absent_capability",
             "expected_tool": None},
        ]
        self.write_golden()
        self.index, self.receipt = self.build()

    def entry(self, tool, verbs, objects):
        cli = tool.removeprefix("vp_").removesuffix("_call").replace("_", "-")
        words = ["mvctl", cli]
        if tool.startswith("vp_window_"):
            words.extend(["--session-id", "<session_id>"])
        return {
            "schema": "vp.capability/v1", "tool": tool, "cli": cli, "json_op": tool,
            "origin": "new", "args_schema_sha256": self.pin["sha256"],
            "ops": [], "risk_class": "preparar", "operator_only": False, "supported": True,
            "route": {"verbs": verbs, "objects": objects, "argv_template": " ".join(words)},
            "source": [dict(self.pin)],
        }

    def write_golden(self):
        self.golden.write_bytes(router.canonical_bytes({
            "schema": "vp.router_golden/v1", "entries": self.cases,
        }))

    def build(self):
        return router.build_index(self.entries, manifest_generation=1, score_min=600,
                                  golden_path=self.golden, source_root=self.root)

    def query(self, query="bring the window to my desktop", **overrides):
        keywords = {
            "index": self.index, "registry": self.entries, "manifest_generation": 1,
            "source_root": self.root, "args": {"session_id": "s-fixture"},
            "context": {"caller": "agent", "agent_instance": "fixture"},
            "request_id": "r-fixture", "dry_run": True, "policy": lambda *args: True,
        }
        return router.vp_tool_route(query, **dict(keywords, **overrides))

    def assert_abstain(self, reply, reason, detail=None):
        reply = json.loads(reply)
        self.assertEqual((reply["verdict"], reply["code"], reply.get("reason")),
                         ("ABSTAIN", "ROUTE_ABSTAIN", reason))
        self.assertEqual(reply.get("detail"), detail)
        self.assertEqual(reply["candidates"], [])

    def assert_exact_match(self):
        reply = json.loads(self.query())
        self.assertEqual(reply["verdict"], "MATCH")
        (candidate,) = reply["candidates"]
        self.assertEqual(candidate["tool"], "vp_window_summon")
        self.assertEqual(candidate["mcp_call"], {"name": "vp_window_summon", "arguments": {
            "request_id": "r-fixture", "dry_run": True, "session_id": "s-fixture",
        }})
        self.assertEqual(candidate["cli"], "mvctl window-summon --session-id s-fixture --dry-run")
        self.assertEqual(candidate["json"], {
            "schema": "vp.control.req/v1", "request_id": "r-fixture", "op": "vp_window_summon",
            "context": {"caller": "agent", "agent_instance": "fixture"},
            "args": {"session_id": "s-fixture"}, "dry_run": True,
        })
        self.assertEqual(candidate["citations"], [self.pin])
        self.assertEqual(candidate["missing_args"], [])

    def test_byte_stability_and_exact_calls_with_pinned_citations(self):
        self.assert_exact_match()
        first = self.query()
        self.assertEqual(first, self.query(registry=reversed(self.entries)))
        self.assertEqual(first, router.canonical_bytes(json.loads(first)))
        rebuilt, receipt = self.build()
        self.assertEqual(rebuilt, self.index)
        self.assertEqual(receipt, self.receipt)

    def test_smallest_safe_margin_and_receipt_pins(self):
        self.assertEqual(self.index["calibration"]["margin_min"], 201)
        self.assertEqual(self.receipt["wrong_match"], 0)
        self.assertEqual(self.receipt["ood_abstain_rate_milli"], 1000)
        self.assertEqual(self.receipt["correct_match"], 2)
        self.assertEqual(self.receipt["golden"]["sha256"], sha256(self.golden.read_bytes()).hexdigest())
        self.assertEqual(self.index["calibration"]["receipt_sha256"],
                         sha256(router.canonical_bytes(self.receipt)).hexdigest())

    def test_out_of_domain_abstains(self):
        self.assert_abstain(self.query("book a flight to the moon"), "absent_capability")
        self.assert_abstain(self.query("display panel", args={}), "ambiguous")

    def test_missing_arguments_never_point_to_forge(self):
        self.assert_abstain(self.query(args={}), "missing_arguments")

    def test_live_registry_and_generation_must_match(self):
        changed = deepcopy(self.entries)
        changed[0]["route"]["verbs"].append("restore")
        self.assert_abstain(self.query(registry=changed), "stale_index")
        self.assert_abstain(self.query(manifest_generation=2), "stale_index")

    def test_rehashed_index_cannot_replace_registry_entry_or_tokenizer(self):
        for change in ("entries", "tokenizer"):
            with self.subTest(change=change):
                index = deepcopy(self.index)
                if change == "entries":
                    index["entries"][0]["route"]["verbs"] = ["restore"]
                else:
                    index["tokenizer"]["sha256"] = "a" * 64
                del index["index_sha256"]
                index["index_sha256"] = sha256(router.canonical_bytes(index)).hexdigest()
                self.assert_abstain(self.query(index=index), "stale_index")

    def test_cli_quotes_literal_and_declines_incomplete_template(self):
        reply = json.loads(self.query(args={"session_id": "literal; value"}))
        self.assertEqual(shlex.split(reply["candidates"][0]["cli"]),
                         ["mvctl", "window-summon", "--session-id", "literal; value", "--dry-run"])
        self.entries[0]["route"]["argv_template"] = "mvctl window-summon"
        self.index, _ = self.build()
        self.assert_abstain(self.query(), "policy_denied")

    def test_changed_citation_blocks_match(self):
        self.source.write_text("Changed source.\n", encoding="utf-8")
        self.assert_abstain(self.query(), "stale_index")
        with self.assertRaisesRegex(ValueError, "Citation"):
            self.build()

    def test_policy_denial_and_unbound_operation(self):
        self.assert_abstain(self.query(policy=lambda *args: False), "policy_denied")
        entry = self.entry("vp_perception_call", [], ["surface"])
        frozen = router._validation._adapter_rows()["vp_perception_call"]["ops"]["status"]
        row = {key: frozen[key] for key in ("supported", "operator_only", "risk_class",
                                          "args_schema_sha256", "reply_schema_sha256", "binding")}
        entry.update(ops=["status"], op_table=[dict(row, op="status")], supported=False,
                     risk_class=frozen["risk_class"])
        self.entries.append(entry)
        self.index, _ = self.build()
        self.assert_abstain(self.query("status surface"), "policy_denied", "op_unbound")

    def add_model(self, state, name, tool):
        entry = self.entry(tool, [name], [])
        entry.update(supported=False, provider={"kind": "local_model", "model": {
            "name": name, "role": "test model",
            "weights": {"format": "safetensors", "bytes_on_disk": 1,
                        "tree_sha256": self.pin["sha256"], "locator": "af://fixture"},
            "deps": [], "device": "cpu", "license": {"id": "fixture", "territory_ok": state != "excluded"},
            "qualification": {"state": state, "reason": "fixture", "receipt": None},
            "measured": [], "residency": {"mode": "cold_process", "lease": "none", "resident_bytes": None},
        }})
        self.entries.append(entry)

    def test_model_details_and_exclusion(self):
        self.assert_abstain(self.query("generate with Hunyuan3D-2.1"),
                            "policy_denied", "model_excluded")
        self.add_model("unqualified", "UniRig", "vp_list_capsules")
        self.add_model("excluded", "Hunyuan3D-2.1", "vp_read_page")
        self.index, _ = self.build()
        self.assertNotIn("Hunyuan3D-2.1", router.canonical_bytes(self.index).decode())
        self.assert_abstain(self.query("auto-rig with UniRig"), "policy_denied", "model_unqualified")
        self.assert_abstain(self.query("generate with Hunyuan3D-2.1"),
                            "policy_denied", "model_excluded")

    def test_calibration_refuses_when_no_safe_margin_exists(self):
        self.cases[2]["request"] = "vp_show"
        self.write_golden()
        with self.assertRaisesRegex(ValueError, "no safe margin"):
            self.build()

    def test_calibration_refuses_all_abstention(self):
        self.cases = self.cases[2:]
        self.write_golden()
        with self.assertRaisesRegex(ValueError, "no safe margin"):
            self.build()

    def assert_read_only(self, query):
        with patch("builtins.open", wraps=open) as open_file, patch(
            "io.open", wraps=io.open
        ) as open_path, patch.object(router, "build_index", side_effect=AssertionError("query rebuilt")):
            query()
        for handle in (open_file, open_path):
            for call in handle.call_args_list:
                mode = call.kwargs.get("mode", call.args[1] if len(call.args) > 1 else "r")
                self.assertTrue(all(flag not in mode for flag in "wax+"))

    def test_query_is_read_only(self):
        self.assert_read_only(self.query)

    def assert_uncalibrated_refused(self):
        mutant = deepcopy(self.index)
        mutant["calibration"]["state"] = "uncalibrated"
        del mutant["index_sha256"]
        mutant["index_sha256"] = sha256(router.canonical_bytes(mutant)).hexdigest()
        with self.assertRaises(ValidationError):
            self.query(index=mutant)

    def test_mutant_uncalibrated_passthrough(self):
        self.assert_uncalibrated_refused()
        validate = router.validate_contract
        with patch.object(router, "validate_contract", side_effect=lambda schema, value:
                          None if schema == router.INDEX else validate(schema, value)):
            with self.assertRaisesRegex(AssertionError, "ValidationError not raised"):
                self.assert_uncalibrated_refused()

    def test_mutant_top_one_without_margin(self):
        def query():
            self.assert_abstain(self.query("summon window panel"), "ambiguous")

        query()
        with patch.object(router, "_margin_passes", return_value=True):
            with self.assertRaises(AssertionError):
                query()
            self.assertEqual(json.loads(self.query("summon window panel"))["verdict"], "MATCH")

    def test_mutant_rebuild_at_query(self):
        self.assert_read_only(self.query)

        def rebuild_then_query():
            self.build()
            return self.query()

        with self.assertRaisesRegex(AssertionError, "query rebuilt"):
            self.assert_read_only(rebuild_then_query)

    def test_mutant_citation_without_digest(self):
        self.assert_exact_match()
        with patch.object(router, "_citations", return_value=[{"path": self.pin["path"],
                          "line_start": 1, "line_end": 1}]):
            with self.assertRaisesRegex(ValidationError, "'sha256' is a required property"):
                self.assert_exact_match()
