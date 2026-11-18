"""Synthetic, bounded F0-U4 tests; runnable under python3 -I."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

# Isolated mode intentionally drops cwd; add this component explicitly.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.manifest import cache


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.store = self.root / "cache"
        self.provider = {"schema": "af.provider/v1", "id": "synthetic-tool", "revision": "frozen-tool-v1", "slot": "generate", "runnable": True, "kind": "tool", "licence": {"code": "MIT", "weights": "MIT", "deps": []}, "territory_excluded": [], "commercial": True, "gated": False, "weights": [], "runtime": {"container": "synthetic/no-runtime", "ram_peak_gb_measured": None, "ram_peak_measurement_ref": None}, "known_issues": []}
        self.request = cache.build_identity(slot="generate", inputs=[{"role": "prompt", "text": "synthetic cart é"}],
            provider=self.provider, params={"seed": 7, "enabled": True}, toolchain_sha256="1" * 64, policy_sha256="2" * 64)
        self.asset = b"synthetic-GLB-bytes\0no-model-output"
        self.subject = digest(self.asset)
        self.bridge = {"schema": "af.bridge-verdict/v1", "rc": 0, "verdict": "PASS", "reason_codes": [],
            "source_sha256": self.subject, "profile": {"id": "synthetic-intake", "sha256": "3" * 64},
            "receipt_file_sha256": "4" * 64, "trace_sha256": "5" * 64, "failed_checks": []}
        self.manifest = {"schema": "af.manifest/v1", "request_key": cache.request_key(self.request),
            "request": deepcopy(self.request), "verdict": "PASS", "artifacts": [
                {"role": "mesh_glb", "path": "assets/cart.glb", "sha256": self.subject, "media_type": "model/gltf-binary"}],
            "gate_refs": [{"gate": "intake", "schema": "af.bridge-verdict/v1", "path": "gates/intake.json",
                "sha256": digest(encoded(self.bridge)), "subject_sha256": self.subject, "verdict": "PASS"}],
            "parent_artifacts": []}
        self.decision = {"schema": "af.licence-decision/v1", "rc": 0, "verdict": "PASS", "allowed": True,
            "provider_id": self.request["provider_id"], "operation": "generate", "jurisdiction": "ES",
            "commercial_use": False, "monetisable": True, "reason_codes": [], "evaluated_dependencies": [],
            "configuration_sha256": "6" * 64, "provider_sha256": self.request["provider_sha256"],
            "policy_sha256": self.request["policy_sha256"]}
        self.write("assets/cart.glb", self.asset)
        self.write("gates/intake.json", encoded(self.bridge))

    def write(self, relative, data, root=None):
        p = (root or self.source) / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def evidence(self, payload):
        data = encoded(payload)
        self.write("gates/intake.json", data)
        self.manifest["gate_refs"][0]["sha256"] = digest(data)

    def publish(self):
        return cache.publish(self.store, self.request, self.manifest, self.source)

    def lookup(self, **kwargs):
        return cache.lookup(self.store, self.request, decision=kwargs.get("decision", self.decision), required_gates=kwargs.get("required_gates", ("intake",)))

    def entry(self):
        return self.store / cache.request_key(self.request)

    def assertResult(self, result, rc, status):
        self.assertEqual((result["rc"], result["cache_status"]), (rc, status), result)
        self.assertNotEqual(result["rc"], 1)
        cache._validate(result, "cache-result")
        if rc != 0 or status == "MISS":
            self.assertEqual(result["artifacts"], [])
        if status != "MISS":
            self.assertEqual(result["verdict"], cache.core.VERDICT[rc])

    def test_key_canonical_mapping_order(self):
        reordered = dict(reversed(list(self.request.items())))
        reordered["params"] = dict(reversed(list(self.request["params"].items())))
        self.assertEqual(cache.request_key(self.request), digest(encoded(self.request)))
        self.assertEqual(cache.request_key(self.request), cache.request_key(reordered))
        reordered["params"]["seed"] += 1
        self.assertNotEqual(cache.request_key(self.request), cache.request_key(reordered))

    def test_key_binds_full_identity(self):
        original = cache.request_key(self.request)
        changes = {"provider_id": "another-tool", "provider_revision": "frozen-tool-v2", "provider_sha256": "a" * 64,
            "toolchain_sha256": "b" * 64, "policy_sha256": "c" * 64,
            "inputs": [{"role": "prompt", "text": "different synthetic cart"}], "params": {"seed": 8}, "slot": "parts"}
        for field, value in changes.items():
            with self.subTest(field=field):
                altered = deepcopy(self.request)
                altered[field] = value
                self.assertNotEqual(original, cache.request_key(altered))
        a = deepcopy(self.request)
        a["inputs"] = [{"role": "image", "path": "image.png", "media_type": "image/png", "sha256": "a" * 64}]
        b = deepcopy(a)
        b["inputs"][0]["sha256"] = "a" * 63 + "b"
        self.assertNotEqual(cache.request_key(a), cache.request_key(b))
        b["inputs"] = list(reversed([a["inputs"][0], {"role": "prompt", "text": "extra"}]))
        a["inputs"] += [{"role": "prompt", "text": "extra"}]
        self.assertNotEqual(cache.request_key(a), cache.request_key(b))
        provider = dict(self.provider, revision="frozen-tool-v2")
        identity = cache.build_identity(slot="generate", inputs=self.request["inputs"], provider=provider,
            params=self.request["params"], toolchain_sha256="1" * 64, policy_sha256="2" * 64)
        self.assertNotEqual(self.request["provider_sha256"], identity["provider_sha256"])

    def test_request_validation(self):
        for field, value in (("provider_revision", "main"), ("provider_revision", "HEAD"),
                ("policy_sha256", "2" * 8), ("toolchain_sha256", True), ("params", {"seed": 1.5}),
                ("params", {"nested": {}}), ("inputs", []), ("slot", "film"), ("extra", "x")):
            with self.subTest(field=field, value=value):
                request = deepcopy(self.request)
                request[field] = value
                with self.assertRaises(cache.core.ContractError):
                    cache.request_key(request)
        copy = deepcopy(self.request)
        self.request["inputs"][0]["text"] = "mutated caller"
        self.assertNotEqual(self.request, copy)
        self.assertEqual(self.manifest["request"], copy)

    def test_missing_entry_is_not_acceptance(self):
        result = self.lookup()
        self.assertResult(result, 0, "MISS")
        self.assertIsNone(result["verdict"])
        self.assertIsNone(result["manifest_sha256"])
        self.assertFalse(self.store.exists())

    def test_pass_roundtrip_requires_bound_receipts(self):
        original = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        self.assertResult(self.publish(), 0, "HIT_PASS")
        result = self.lookup()
        self.assertResult(result, 0, "HIT_PASS")
        self.assertEqual(self.publish(), result)
        self.assertEqual(result["manifest_sha256"], digest(encoded(self.manifest)))
        self.assertEqual(Path(result["artifacts"][0]["path"]).read_bytes(), self.asset)
        self.assertEqual((self.entry() / "manifest.json").read_bytes(), encoded(self.manifest))
        for relative, data in original.items():
            self.assertEqual((self.source / relative).read_bytes(), data)
        self.bridge["source_sha256"] = "a" * 64
        self.evidence(self.bridge)
        self.assertResult(cache.publish(self.root / "different", self.request, self.manifest, self.source), 4, "STALE")

    def test_reject_then_repeat_stays_rejected(self):
        self.manifest["verdict"] = "FAIL"
        self.assertResult(self.publish(), 5, "HIT_REJECTED")
        for _ in range(2):
            self.assertResult(self.lookup(), 5, "HIT_REJECTED")
        stored = (self.entry() / "manifest.json").read_bytes()
        self.manifest["verdict"] = "PASS"
        self.assertResult(self.publish(), 2, "ERROR")
        self.assertEqual((self.entry() / "manifest.json").read_bytes(), stored)

    def test_missing_or_unclear_evidence_cannot_pass(self):
        self.assertResult(self.publish(), 0, "HIT_PASS")
        for gates in ((), ("unobserved",), ("intake", "unobserved")):
            self.assertResult(self.lookup(required_gates=gates), 3, "CANNOT_SEE")
        (self.entry() / "gates/intake.json").unlink()
        self.assertResult(self.lookup(), 3, "CANNOT_SEE")
        for verdict in ("SKIP", "UNCLEAR", "CANNOT_SEE", "ADVISORY", "INCOMPLETE"):
            with self.subTest(verdict=verdict):
                self.manifest["verdict"] = verdict
                self.assertResult(cache.publish(self.root / verdict, self.request, self.manifest, self.source), 3, "CANNOT_SEE")

    def test_bridge_cannot_see_and_unknown_evidence(self):
        self.bridge.update(rc=3, verdict="CANNOT_SEE", reason_codes=["missing_check"], trace_sha256=None, receipt_file_sha256=None)
        self.manifest["gate_refs"][0]["verdict"] = "CANNOT_SEE"
        self.evidence(self.bridge)
        self.assertResult(self.publish(), 3, "CANNOT_SEE")
        self.assertResult(self.lookup(), 3, "CANNOT_SEE")
        self.bridge.update(rc=0, verdict="PASS", reason_codes=[], trace_sha256="5" * 64, receipt_file_sha256="4" * 64)
        self.bridge["schema"] = "af.unknown-evaluator/v1"
        self.manifest["gate_refs"][0]["schema"] = self.bridge["schema"]
        self.manifest["gate_refs"][0]["verdict"] = "PASS"
        self.evidence(self.bridge)
        self.assertResult(cache.publish(self.root / "unknown", self.request, self.manifest, self.source), 3, "CANNOT_SEE")

    def test_tampered_artifact_or_receipt_is_stale(self):
        self.assertResult(self.publish(), 0, "HIT_PASS")
        for relative in ("assets/cart.glb", "gates/intake.json"):
            with self.subTest(relative=relative):
                target = self.entry() / relative
                original = target.read_bytes()
                target.write_bytes(original + b"tamper")
                self.assertResult(self.lookup(), 4, "STALE")
                target.write_bytes(original)
        (self.entry() / "assets/cart.glb").unlink()
        self.assertResult(self.lookup(), 4, "STALE")

    def test_report_fail_maps_to_stage_five(self):
        report = {"schema": "vqa.report/v1", "verdict": "FAIL", "rc": 1}
        self.manifest["gate_refs"][0]["schema"] = "vqa.report/v1"
        # A forged manifest PASS cannot override an upstream report FAIL.
        self.evidence(report)
        self.assertResult(self.publish(), 5, "HIT_REJECTED")
        self.assertResult(self.lookup(), 5, "HIT_REJECTED")
        self.assertEqual((self.entry() / "gates/intake.json").read_bytes(), encoded(report))

    def test_report_pass_requires_bridge(self):
        self.manifest["gate_refs"][0]["schema"] = "vqa.report/v1"
        self.evidence({"schema": "vqa.report/v1", "verdict": "PASS", "rc": 0})
        self.assertResult(self.publish(), 3, "CANNOT_SEE")

    def test_crash_before_manifest_not_visible(self):
        original_copy = cache._copy
        for fail_at in (1, 2):
            calls = [0]
            def crash(*args):
                original_copy(*args)
                self.assertResult(self.lookup(), 0, "MISS")
                calls[0] += 1
                if calls[0] == fail_at:
                    raise OSError("synthetic crash")
            with self.subTest(copy=fail_at), patch.object(cache, "_copy", crash):
                self.assertResult(self.publish(), 2, "ERROR")
            self.assertResult(self.lookup(), 0, "MISS")
        with patch.object(cache.core, "atomic_write_json", side_effect=OSError("manifest crash")):
            self.assertResult(self.publish(), 2, "ERROR")
        self.assertResult(self.lookup(), 0, "MISS")
        with patch.object(cache.os, "replace", side_effect=OSError("rename crash")):
            self.assertResult(self.publish(), 2, "ERROR")
        self.assertResult(self.lookup(), 0, "MISS")
        self.assertFalse(list(self.store.glob(".stage-*")))
        self.assertFalse(list(self.store.glob(".publish-*")))
        self.assertResult(self.publish(), 0, "HIT_PASS")

    def test_staging_manifest_is_last(self):
        real_write = cache.core.atomic_write_json
        def check(target, obj):
            for ref in self.manifest["artifacts"] + self.manifest["gate_refs"]:
                self.assertEqual(digest((target.parent / ref["path"]).read_bytes()), ref["sha256"])
            self.assertResult(self.lookup(), 0, "MISS")
            real_write(target, obj)
            self.assertResult(self.lookup(), 0, "MISS")
        with patch.object(cache.core, "atomic_write_json", check):
            self.assertResult(self.publish(), 0, "HIT_PASS")

    def test_incomplete_entry_never_visible_or_overwritten(self):
        self.write("assets/cart.glb", self.asset, root=self.entry())
        before = (self.entry() / "assets/cart.glb").read_bytes()
        self.assertResult(self.lookup(), 0, "MISS")
        self.assertResult(self.publish(), 2, "ERROR")
        self.assertEqual((self.entry() / "assets/cart.glb").read_bytes(), before)

    def test_conflicting_publish_and_path_escape_refused(self):
        self.assertResult(self.publish(), 0, "HIT_PASS")
        original = (self.entry() / "manifest.json").read_bytes()
        self.manifest["parent_artifacts"] = [{"role": "raw", "sha256": "a" * 64}]
        self.assertResult(self.publish(), 2, "ERROR")
        self.assertEqual((self.entry() / "manifest.json").read_bytes(), original)
        for path in ("../outside.glb", "/tmp/outside.glb", "assets/../outside.glb", "assets//cart.glb", "manifest.json"):
            with self.subTest(path=path):
                self.manifest["artifacts"][0]["path"] = path
                self.assertResult(cache.publish(self.root / "escape", self.request, self.manifest, self.source), 2, "ERROR")
        self.assertEqual((self.entry() / "manifest.json").read_bytes(), original)

    def test_escaping_symlink_refused(self):
        outside = self.write("outside/cart.glb", self.asset, root=self.root)
        asset = self.source / "assets/cart.glb"
        asset.unlink()
        asset.symlink_to(outside)
        self.assertResult(self.publish(), 2, "ERROR")
        asset.unlink()
        asset.write_bytes(self.asset)
        self.assertResult(self.publish(), 0, "HIT_PASS")
        stored = self.entry() / "assets/cart.glb"
        stored.unlink()
        stored.symlink_to(outside)
        self.assertResult(self.lookup(), 2, "ERROR")
        marker = self.entry() / "manifest.json"
        before = marker.read_bytes()
        marker.unlink()
        external = self.write("external.json", before, root=self.root)
        marker.symlink_to(external)
        self.assertResult(self.lookup(), 2, "ERROR")

    def test_concurrent_publishers_and_lease(self):
        entered, proceed = threading.Event(), threading.Event()
        real_copy = cache._copy
        def pause(*args):
            if not entered.is_set():
                entered.set()
                self.assertTrue(proceed.wait(3))
            return real_copy(*args)
        with patch.object(cache, "_copy", pause), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.publish)
            self.assertTrue(entered.wait(3))
            changed = deepcopy(self.manifest)
            changed["verdict"] = "FAIL"
            try:
                second = cache.publish(self.store, self.request, changed, self.source)
            finally:
                proceed.set()
            self.assertResult(second, 2, "ERROR")
            self.assertResult(first.result(timeout=3), 0, "HIT_PASS")
        self.assertResult(self.lookup(), 0, "HIT_PASS")
        self.assertFalse(list(self.store.glob(".publish-*")))

    def test_derivative_preserves_raw_identity(self):
        self.assertResult(self.publish(), 0, "HIT_PASS")
        raw_path = self.entry() / "assets/cart.glb"
        raw_marker = (self.entry() / "manifest.json").read_bytes()
        self.request["params"]["repair"] = True
        candidate = b"synthetic repaired derivative"
        self.manifest.update(request=deepcopy(self.request), request_key=cache.request_key(self.request),
            parent_artifacts=[{"role": "raw", "sha256": self.subject}])
        self.manifest["artifacts"][0]["sha256"] = digest(candidate)
        self.manifest["gate_refs"][0]["subject_sha256"] = digest(candidate)
        self.write("assets/cart.glb", candidate)
        # Copying the parent's receipt fails even if its bytes are still intact.
        self.assertResult(self.publish(), 4, "STALE")
        self.bridge["source_sha256"] = digest(candidate)
        self.evidence(self.bridge)
        self.assertResult(self.publish(), 0, "HIT_PASS")
        self.assertEqual(raw_path.read_bytes(), self.asset)
        self.assertEqual((raw_path.parent.parent / "manifest.json").read_bytes(), raw_marker)
        self.assertNotEqual(raw_path.parent.parent, self.entry())

    def test_policy_rechecked_on_lookup(self):
        self.assertResult(self.publish(), 0, "HIT_PASS")
        denied = dict(self.decision, allowed=False, rc=5, verdict="FAIL", reason_codes=["territory_excluded"])
        advisory = deepcopy(denied)
        hit = self.lookup(decision=denied)
        self.assertResult(hit, 0, "HIT_PASS")
        self.assertEqual(hit, self.lookup())
        self.assertEqual(denied, advisory)
        for field in ("provider_sha256", "policy_sha256", "provider_id", "operation"):
            decision = deepcopy(self.decision)
            decision[field] = "a" * 64 if field.endswith("sha256") else "different"
            with self.subTest(field=field):
                self.assertResult(self.lookup(decision=decision), 4, "STALE")
        self.assertResult(self.lookup(), 0, "HIT_PASS")
        self.request["policy_sha256"] = "a" * 64
        self.decision["policy_sha256"] = "a" * 64
        self.assertResult(self.lookup(), 0, "MISS")

    def test_decision_malformed_is_error(self):
        self.assertResult(self.publish(), 0, "HIT_PASS")
        for field, value in (("allowed", "true"), ("rc", True), ("rc", 1), ("commercial_use", "invalid"), ("configuration_sha256", None)):
            decision = dict(self.decision)
            decision[field] = value
            with self.subTest(field=field):
                self.assertResult(self.lookup(decision=decision), 2, "ERROR")

    def test_schema_and_evidence_negatives(self):
        for field, value in (("gate_refs", []), ("verdict", "OK"), ("schema", "af.manifest/v2"), ("extra", 1)):
            manifest = deepcopy(self.manifest)
            manifest[field] = value
            with self.subTest(field=field):
                self.assertResult(cache.publish(self.store, self.request, manifest, self.source), 2, "ERROR")
        for field, value in (("rc", True), ("rc", 1), ("profile", {}), ("trace_sha256", None), ("failed_checks", ["hidden_fail"])):
            bridge = dict(self.bridge)
            bridge[field] = value
            self.evidence(bridge)
            with self.subTest(field=field):
                self.assertResult(self.publish(), 2, "ERROR")
        self.assertFalse(self.store.exists())

    def test_manifest_and_gate_bindings(self):
        self.manifest["gate_refs"][0]["subject_sha256"] = "a" * 64
        self.bridge["source_sha256"] = "a" * 64
        self.evidence(self.bridge)
        self.assertResult(self.publish(), 3, "CANNOT_SEE")
        self.manifest["gate_refs"][0]["subject_sha256"] = self.subject
        self.bridge["source_sha256"] = self.subject
        self.evidence(self.bridge)
        self.manifest["gate_refs"].append(deepcopy(self.manifest["gate_refs"][0]))
        self.assertResult(cache.publish(self.root / "duplicate", self.request, self.manifest, self.source), 2, "ERROR")
        self.manifest["gate_refs"].pop()
        self.manifest["request_key"] = "a" * 64
        self.assertResult(cache.publish(self.root / "drift", self.request, self.manifest, self.source), 4, "STALE")

    def test_duplicate_json_keys_and_float_rejected(self):
        valid = encoded(self.bridge)
        for data in (valid.rstrip()[:-1] + b',"rc":0}\n', valid.replace(b'"rc":0', b'"rc":0.0'), valid.replace(b'"rc":0', b'"rc":NaN')):
            self.write("gates/intake.json", data)
            self.manifest["gate_refs"][0]["sha256"] = digest(data)
            self.assertResult(self.publish(), 2, "ERROR")

    def test_resource_bounds_are_not_pass(self):
        for field, value in (("MAX_TOTAL_BYTES", 1), ("MAX_JSON_BYTES", 1), ("TIMEOUT_S", 0)):
            with self.subTest(field=field), patch.object(cache, field, value):
                result = self.publish()
                self.assertResult(result, 6, "BUDGET_EXCEEDED")
        self.assertFalse((self.entry() / "manifest.json").exists())
        self.assertResult(self.publish(), 0, "HIT_PASS")
        with patch.object(cache, "MAX_TOTAL_BYTES", 1):
            self.assertResult(self.lookup(), 6, "BUDGET_EXCEEDED")

    def test_payload_copy_drift_and_io_preserve_source(self):
        real_copy = cache._copy
        original = (self.source / "assets/cart.glb").read_bytes()
        def drift(source, target, expected, budget):
            source.write_bytes(source.read_bytes() + b"external-change")
            return real_copy(source, target, expected, budget)
        with patch.object(cache, "_copy", drift):
            self.assertResult(self.publish(), 4, "STALE")
        self.write("assets/cart.glb", original)
        with patch.object(cache, "_sync_dir", side_effect=OSError("disk error")):
            self.assertResult(self.publish(), 2, "ERROR")
        self.assertEqual((self.source / "assets/cart.glb").read_bytes(), original)
        self.assertResult(self.lookup(), 0, "MISS")

    def test_null_source_and_specific_bridge_codes(self):
        for rc in (2, 3, 4, 5, 6):
            with self.subTest(rc=rc):
                self.bridge.update(rc=rc, verdict=cache.core.VERDICT[rc], source_sha256=None)
                self.manifest["gate_refs"][0]["verdict"] = cache.core.VERDICT[rc]
                self.evidence(self.bridge)
                self.assertResult(cache.publish(self.root / ("rc-" + str(rc)), self.request, self.manifest, self.source), rc, cache._STATES[rc])

    def test_foreign_error_and_gate_error_dominate_fail(self):
        self.manifest["verdict"] = "FAIL"
        self.manifest["gate_refs"][0]["schema"] = "vqa.report/v1"
        self.evidence({"schema": "vqa.report/v1", "rc": 2, "verdict": "ERROR"})
        self.assertResult(self.publish(), 2, "ERROR")
        self.manifest["gate_refs"][0]["schema"] = "af.bridge-verdict/v1"
        self.manifest["gate_refs"][0]["verdict"] = "ERROR"
        self.evidence(self.bridge)
        self.assertResult(self.publish(), 2, "ERROR")

    def test_embedded_request_bool_integer_drift(self):
        self.manifest["request"]["params"]["enabled"] = 1
        self.assertResult(self.publish(), 4, "STALE")
        self.assertFalse(self.store.exists())

    def test_refusal_io_and_cleanup_errors(self):
        with patch.object(cache, "_copy", side_effect=OSError("copy error")), patch.object(cache.shutil, "rmtree", side_effect=OSError("cleanup error")):
            result = self.publish()
        self.assertResult(result, 2, "ERROR")
        self.assertEqual(result["reason_codes"], ["cleanup_failed"])
        self.assertResult(self.lookup(), 0, "MISS")

    def test_file_type_and_duplicate_gate_names(self):
        self.assertResult(self.publish(), 0, "HIT_PASS")
        self.assertResult(self.lookup(required_gates=("intake", "intake")), 2, "ERROR")
        artifact = self.entry() / "assets/cart.glb"
        artifact.unlink()
        artifact.mkdir()
        self.assertResult(self.lookup(), 2, "ERROR")


if __name__ == "__main__":
    unittest.main()
