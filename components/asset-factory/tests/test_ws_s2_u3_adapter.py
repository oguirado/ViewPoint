"""S2-U3 contract qualification; synthetic files never qualify live sculpting."""

import copy
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from asset_factory import core, contracts
from providers.blender_sculpt import adapter


class SculptAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = adapter.load_provider()
        self.request = {
            "schema": "af.refine/v1", "kind": "request", "slot": "refine",
            "request_id": "synthetic-refine", "provider_id": "blender_sculpt",
            "provider_revision": self.provider["revision"], "inputs": [], "params": {},
            "context": {"jurisdiction": "ES", "commercial_use": False,
                        "policy_sha256": "a" * 64, "toolchain_sha256": "b" * 64,
                        "timeout_s": 10},
        }
        for role, name, data, media in (
            ("coarse_glb", "raw.glb", b"synthetic geometry, not a qualified GLB", "model/gltf-binary"),
            ("reference_image", "reference.png", b"synthetic reference, not a qualified image", "image/png"),
        ):
            (self.root / name).write_bytes(data)
            self.request["inputs"].append({"role": role, "path": name,
                                          "media_type": media,
                                          "sha256": hashlib.sha256(data).hexdigest()})

    def run_adapter(self, request=None, **kwargs):
        return adapter.refine(self.request if request is None else request,
                              input_root=self.root, **kwargs)

    def assert_refused(self, rc, reason, request=None, **kwargs):
        result = self.run_adapter(request, **kwargs)
        self.assertEqual(result["rc"], rc)
        self.assertEqual(result["status"], core.STATUS[rc])
        if result.get("schema") == "af.refine/v1":
            contracts.validate_slot(result, "refine", "result")
            self.assertEqual(result["outputs"], [])
            self.assertEqual(result["receipts"], [])
            self.assertEqual(result["absence_reasons"]["execution"], reason)
        else:
            self.assertEqual(result["reason_codes"], [reason])
        self.assertNotIn("verdict", result)
        return result

    def test_manifest_valid_tool_on_existing_refine_slot(self):
        contracts.validate_provider(self.provider)
        self.assertEqual(self.provider["schema"], "af.provider/v1")
        self.assertEqual(self.provider["slot"], "refine")
        self.assertEqual(self.provider["kind"], "tool")
        self.assertEqual(self.provider["weights"], [])
        self.assertFalse(self.provider["runnable"])
        self.assertIsNone(self.provider["runtime"]["ram_peak_gb_measured"])
        self.assertIsNone(self.provider["runtime"]["ram_peak_measurement_ref"])
        self.assertEqual(self.provider["revision"], "sha256:" + core.sha256_file(Path(adapter.__file__)))
        stored = Path(adapter.__file__).with_name("provider.json").read_text()
        self.assertEqual(stored, core.canonical_json(self.provider))

    def test_manifest_revision_main_is_exact_negative_control(self):
        broken = copy.deepcopy(self.provider)
        broken["revision"] = "main"
        with self.assertRaises(core.ContractError) as caught:
            contracts.validate_provider(broken)
        self.assertEqual(caught.exception.code, "unpinned_revision")
        self.assertEqual(caught.exception.rc, 2)
        contracts.validate_provider(self.provider)

    def test_all_mutable_manifest_revisions_refused(self):
        for revision in ("main", "master", "latest", "HEAD"):
            with self.subTest(revision=revision):
                broken = copy.deepcopy(self.provider)
                broken["revision"] = revision
                with self.assertRaises(core.ContractError) as caught:
                    contracts.validate_provider(broken)
                self.assertEqual(caught.exception.code, "unpinned_revision")

    def test_default_never_claims_synthetic_runtime_readiness(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        original = copy.deepcopy(self.request)
        self.assert_refused(3, "sculpt_execution_unqualified")
        self.assertEqual(self.request, original)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_request_main_is_error_before_execution(self):
        self.request["provider_revision"] = "main"
        self.assert_refused(2, "unpinned_revision")

    def test_request_revision_drift_is_stale(self):
        self.request["provider_revision"] = "immutable-but-wrong"
        self.assert_refused(4, "provider_stale")

    def test_other_provider_is_error(self):
        self.request["provider_id"] = "other"
        self.assert_refused(2, "provider_mismatch")

    def test_other_slot_is_error(self):
        self.request["slot"] = "generate"
        self.assertEqual(self.run_adapter()["rc"], 2)

    def test_missing_reference_and_duplicate_roles_are_errors(self):
        self.request["inputs"].pop()
        self.assert_refused(2, "input_exclusivity")
        self.request["inputs"].append(copy.deepcopy(self.request["inputs"][0]))
        self.assert_refused(2, "input_exclusivity")

    def test_float_and_boolean_timeout_are_errors(self):
        self.request["params"]["strength"] = 0.5
        self.assert_refused(2, "non_integer_number")
        self.request["params"].clear()
        self.request["context"]["timeout_s"] = True
        self.assertEqual(self.run_adapter()["rc"], 2)

    def test_changed_raw_and_reference_bytes_are_stale(self):
        for item in self.request["inputs"]:
            with self.subTest(role=item["role"]):
                path = self.root / item["path"]
                original = path.read_bytes()
                path.write_bytes(original + b"tampered")
                self.assert_refused(4, "input_stale")
                path.write_bytes(original)

    def test_missing_input_cannot_see(self):
        (self.root / "raw.glb").unlink()
        self.assert_refused(3, "input_missing")

    def test_parent_traversal_and_absolute_escape_are_errors(self):
        for path in ("../outside.glb", str(self.root.parent / "outside.glb")):
            with self.subTest(path=path):
                self.request["inputs"][0]["path"] = path
                self.assert_refused(2, "path_escape")

    def test_escaping_symlink_is_error(self):
        (self.root / "escape.glb").symlink_to(self.root.parent / "outside.glb")
        self.request["inputs"][0]["path"] = "escape.glb"
        self.assert_refused(2, "path_escape")

    def test_non_regular_input_is_error(self):
        self.request["inputs"][0]["path"] = "."
        self.assert_refused(2, "input_not_regular")

    def test_size_limit_is_budget_exceeded(self):
        with mock.patch.object(adapter, "MAX_INPUT_BYTES", 1):
            self.assert_refused(6, "input_budget_exceeded")

    def test_elapsed_deadline_is_budget_exceeded(self):
        with mock.patch.object(adapter.time, "monotonic_ns", side_effect=[0, 11_000_000_000, 11_000_000_000]):
            self.assert_refused(6, "deadline_exceeded")

    def test_runnable_flag_cannot_manufacture_an_executor(self):
        changed = copy.deepcopy(self.provider)
        changed["runnable"] = True
        contracts.validate_provider(changed)
        with mock.patch.object(adapter, "load_provider", return_value=changed):
            self.assert_refused(3, "sculpt_execution_unqualified")

    def test_code_pin_drift_is_stale(self):
        with mock.patch.object(adapter.core, "sha256_file", return_value="f" * 64):
            self.assert_refused(4, "adapter_stale")

    def test_nonmapping_request_returns_error(self):
        for bad in ([], None, "bad", {"schema": "af.refine/v1"}):
            with self.subTest(value=bad):
                result = adapter.refine(bad, input_root=self.root)
                self.assertEqual(result["rc"], 2)

    def test_cli_refusal_preserves_existing_result(self):
        source = self.root / "request.json"
        source.write_text(core.canonical_json(self.request))
        target = self.root / "caller.json"
        target.write_bytes(b"caller-owned bytes")
        output = io.StringIO()
        with redirect_stdout(output):
            rc = adapter.main(["--request", str(source), "--input-root", str(self.root),
                               "--result", str(target)])
        self.assertEqual(rc, 3)
        self.assertEqual(target.read_bytes(), b"caller-owned bytes")
        parsed = json.loads(output.getvalue())
        contracts.validate_slot(parsed, "refine", "result")
        self.assertEqual(output.getvalue(), core.canonical_json(parsed))

    def test_cli_refusal_never_creates_result(self):
        source = self.root / "request.json"
        source.write_text(core.canonical_json(self.request))
        target = self.root / "absent" / "caller.json"
        with redirect_stdout(io.StringIO()):
            rc = adapter.main(["--request", str(source), "--input-root", str(self.root),
                               "--result", str(target)])
        self.assertEqual(rc, 3)
        self.assertFalse(target.parent.exists())

    def test_cli_duplicate_key_is_error_preserving_result(self):
        source = self.root / "request.json"
        source.write_text('{"schema":"af.refine/v1","schema":"af.refine/v1"}')
        target = self.root / "caller.json"
        target.write_bytes(b"caller bytes")
        with redirect_stdout(io.StringIO()):
            rc = adapter.main(["--request", str(source), "--input-root", str(self.root),
                               "--result", str(target)])
        self.assertEqual(rc, 2)
        self.assertEqual(target.read_bytes(), b"caller bytes")

    def test_cli_usage_never_returns_one(self):
        for argv in ([], ["--unknown"], ["--help"]):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()):
                self.assertEqual(adapter.main(argv), 2)


if __name__ == "__main__":
    unittest.main()
