"""Additive W3 contracts: independent role oracles and refusal controls."""

import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asset_factory import contracts, core
from tests.test_f0u1_contracts import MEDIA_SCHEMAS


LEGACY = ("generate", "refine", "topology", "uv_texture", "parts", "rig", "motion", "render")
ROLES = {
    "cad_code": (("cad_source",), ("cad_brep", "cad_mesh")),
    "collision": (("visual_glb",), ("collision_glb", "collision_manifest")),
    "physics": (("physics_scene", "physics_protocol"), ("physics_trace", "physics_receipt")),
    "procedural": (("recipe",), ("scene_blend", "scene_glb")),
    "scene_compose": (("asset_set", "layout_recipe"), ("scene_blend", "scene_usd")),
    "denoise": (("frame_set",), ("denoised_frame_set", "denoise_receipt")),
    # DESIGN_CHOICE: image-conditioned texturing has the existing UV/PBR roles.
    "texture": (("glb", "reference_image"), ("textured_glb", "albedo", "normal", "orm")),
}
H = "a" * 64


def artifact(role):
    return {"role": role, "path": role + ".bin", "media_type": "application/octet-stream", "sha256": H}


def request(slot):
    return {"schema": "af." + slot.replace("_", "-") + "/v1", "kind": "request", "slot": slot,
            "request_id": "synthetic-request", "provider_id": "synthetic-tool", "provider_revision": "b" * 40,
            "inputs": [artifact(role) for role in ROLES[slot][0]], "params": {"seed": 42},
            "context": {"jurisdiction": "ES", "commercial_use": False, "policy_sha256": H,
                        "toolchain_sha256": H, "timeout_s": 10}}


def result(slot, rc=0):
    p = request(slot)
    for key in ("inputs", "params", "context"):
        del p[key]
    p.update(kind="result", rc=rc, status=core.STATUS[rc],
             outputs=[artifact(role) for role in ROLES[slot][1]] if rc == 0 else [],
             receipts=[{"schema": "af.synthetic/v1", "path": "receipt.json", "sha256": H}] if rc == 0 else [],
             metrics={"wall_ms": None, "peak_rss_bytes": None},
             absence_reasons={"wall_ms": "not measured", "peak_rss_bytes": "not measured"})
    return p


def provider(slot):
    return {"schema": "af.provider/v1", "id": "synthetic-tool", "slot": slot, "kind": "tool",
            "runnable": False, "blocked_reason": "research_only_unverified_runtime", "revision": None,
            "licence": {"code": "MIT", "weights": "N/A", "deps": []}, "territory_excluded": [],
            "commercial": None, "gated": None, "weights": [],
            "runtime": {"container": None, "ram_peak_gb_measured": None, "ram_peak_measurement_ref": None},
            "known_issues": ["synthetic fixture; no runtime qualification"]}


class W3ContractTests(unittest.TestCase):
    def reject(self, payload, slot, kind="request", code=None):
        before = copy.deepcopy(payload)
        with self.assertRaises(core.ContractError) as cm:
            contracts.validate_slot(payload, slot, kind)
        self.assertEqual(cm.exception.rc, 2)
        if code:
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(payload, before)

    def test_all_new_requests_and_results(self):
        for slot in ROLES:
            for kind, make in (("request", request), ("result", result)):
                with self.subTest(slot=slot, kind=kind):
                    p = make(slot)
                    before = copy.deepcopy(p)
                    self.assertIsNone(contracts.validate_slot(p, slot, kind))
                    self.assertEqual(p, before)
                    schema = core.load_json(ROOT / "slots" / (slot + ".schema.json"), max_bytes=2**20)
                    self.assertIsNone(core.validate(p, schema))

    def test_unknown_slot_and_provider_metadata_are_not_stages(self):
        class SlotName(str):
            pass

        for slot in ("provider", "unregistered_slot", "cad-code", "texture_typo", None, False, 1, [], {},
                     SlotName("texture"), SlotName("generate")):
            self.reject(request("texture"), slot, code="unknown_slot")
        self.reject(request("physics"), "physics", "unknown", "unknown_kind")

    def test_dispatch_role_tables_are_deeply_immutable(self):
        for table in (contracts._INPUTS, contracts._OUTPUTS):
            before = dict(table)
            with self.assertRaises(TypeError):
                table["phantom"] = (frozenset(("wrong",)),)
            with self.assertRaises(AttributeError):
                table["texture"][0].add("wrong")
            self.assertEqual(dict(table), before)

    def test_schema_slot_kind_bindings(self):
        for slot in ROLES:
            for key, value in (("schema", "af.wrong/v1"), ("schema", "af." + slot + "/v2"),
                               ("slot", "generate"), ("kind", "result")):
                with self.subTest(slot=slot, key=key, value=value):
                    p = request(slot); p[key] = value
                    self.reject(p, slot)
            self.reject(request(slot), slot, "result")

    def test_required_and_unknown_fields(self):
        for slot in ROLES:
            for kind, make in (("request", request), ("result", result)):
                for field in make(slot):
                    with self.subTest(slot=slot, kind=kind, field=field):
                        p = make(slot); del p[field]
                        self.reject(p, slot, kind)
                p = make(slot); p["undeclared"] = True
                self.reject(p, slot, kind)
            for key in ("inputs", "context"):
                p = request(slot)
                target = p[key][0] if key == "inputs" else p[key]
                target["undeclared"] = True
                self.reject(p, slot)

    def test_exact_role_coverage_and_uniqueness(self):
        for slot in ROLES:
            for kind, make, key in (("request", request, "inputs"), ("result", result, "outputs")):
                for index in range(len(make(slot)[key])):
                    p = make(slot); p[key].pop(index)
                    self.reject(p, slot, kind)
                p = make(slot); p[key].append(copy.deepcopy(p[key][0]))
                self.reject(p, slot, kind)
                p = make(slot); p[key][0]["role"] = "unregistered_role"
                self.reject(p, slot, kind)
                if len(make(slot)[key]) > 1:
                    p = make(slot); p[key][1] = copy.deepcopy(p[key][0])
                    self.reject(p, slot, kind)

    def test_revision_pins_and_digest_syntax(self):
        for slot in ROLES:
            for revision in ("main", "master", "latest", "HEAD", "", " "):
                p = request(slot); p["provider_revision"] = revision
                self.reject(p, slot, code="unpinned_revision")
            for digest in ("", "a" * 63, "A" * 64, "g" * 64):
                p = request(slot); p["inputs"][0]["sha256"] = digest
                self.reject(p, slot)
            for key in ("policy_sha256", "toolchain_sha256"):
                p = request(slot); p["context"][key] = "unbound"
                self.reject(p, slot)
            p = result(slot); p["receipts"][0]["sha256"] = "unbound"
            self.reject(p, slot, "result")

    def test_integer_only_params_and_explicit_context(self):
        for slot in ROLES:
            for value in (1.5, None, [], {}):
                p = request(slot); p["params"]["bad"] = value
                self.reject(p, slot)
            for value in (True, 0, -1, 1.5):
                p = request(slot); p["context"]["timeout_s"] = value
                self.reject(p, slot)
            for value in (None, 0, "false"):
                p = request(slot); p["context"]["commercial_use"] = value
                self.reject(p, slot)
            p = request(slot); del p["context"]["commercial_use"]
            self.reject(p, slot)

    def test_all_stage_rcs_and_cannot_see_are_preserved(self):
        for slot in ROLES:
            for rc in core.RC:
                p = result(slot, rc)
                self.assertIsNone(contracts.validate_slot(p, slot, "result"))
                self.assertNotIn("verdict", p)
                self.assertEqual(p["rc"], rc)
            for bad_rc in (1, True, -1, 7):
                p = result(slot); p["rc"] = bad_rc
                self.reject(p, slot, "result")
            p = result(slot); p["status"] = "FAIL"
            self.reject(p, slot, "result")
            p = result(slot); p["receipts"] = []
            self.reject(p, slot, "result")

    def test_absent_metrics_require_reasons(self):
        for slot in ROLES:
            for key in ("wall_ms", "peak_rss_bytes"):
                p = result(slot); del p["absence_reasons"][key]
                self.reject(p, slot, "result", "null_metric_without_reason")
                for value in (True, -1, 0.5):
                    p = result(slot); p["metrics"][key] = value
                    self.reject(p, slot, "result")

    def test_draft_providers_and_dependency_slot_enums(self):
        for slot in ROLES:
            p = provider(slot); before = copy.deepcopy(p)
            self.assertIsNone(contracts.validate_provider(p))
            self.assertEqual(p, before)
            self.assertIs(p["runnable"], False)
            p["licence"]["deps"] = [{"name": "synthetic-dependency", "licence": "MIT", "commercial": None,
                "territory_excluded": [], "gated": None, "used_by": [slot]}]
            self.assertIsNone(contracts.validate_provider(p))
            p["licence"]["deps"][0]["used_by"] = ["unregistered_slot"]
            with self.assertRaises(core.ContractError):
                contracts.validate_provider(p)

    def test_draft_cannot_be_promoted_by_flipping_runnable(self):
        for slot in ROLES:
            p = provider(slot); p["runnable"] = True
            with self.assertRaises(core.ContractError) as cm:
                contracts.validate_provider(p)
            self.assertEqual(cm.exception.rc, 2)
            for revision in ("main", "master", "latest", "HEAD"):
                p = provider(slot); p["revision"] = revision
                with self.assertRaises(core.ContractError) as cm:
                    contracts.validate_provider(p)
                self.assertEqual(cm.exception.code, "unpinned_revision")

    def test_provider_metadata_and_unknown_slots_are_rejected(self):
        for slot in ("provider", "unregistered_slot"):
            with self.assertRaises(core.ContractError) as cm:
                contracts.validate_provider(provider(slot))
            self.assertEqual(cm.exception.rc, 2)
            self.assertEqual(cm.exception.code, "schema_mismatch")

    def test_schema_inventory_exact_expansion_and_identity(self):
        expected = set(LEGACY) | set(ROLES)
        self.assertEqual(tuple(contracts.SLOTS), LEGACY + tuple(ROLES))
        self.assertEqual(tuple(core.SLOTS), LEGACY)  # This lane must not mutate the kernel.
        schemas = {p.name.removesuffix(".schema.json"): json.loads(p.read_text())
                   for p in (ROOT / "slots").glob("*.schema.json")}
        self.assertEqual(set(schemas), expected | {"provider"} | MEDIA_SCHEMAS.keys())
        self.assertEqual(set(schemas["provider"]["properties"]["slot"]["enum"]), expected)
        deps = schemas["provider"]["properties"]["licence"]["properties"]["deps"]
        self.assertEqual(set(deps["items"]["properties"]["used_by"]["items"]["enum"]), expected)
        for slot in ROLES:
            s = schemas[slot]
            self.assertEqual(s["$id"], "urn:af:" + slot.replace("_", "-") + ":v1")
            self.assertEqual(s["$defs"]["request"]["properties"]["schema"]["const"], request(slot)["schema"])
            self.assertEqual(s["$defs"]["result"]["properties"]["schema"]["const"], result(slot)["schema"])
            self.assertEqual((ROOT / "slots" / (slot + ".schema.json")).read_text(), core.canonical_json(s))

    def test_cli_refusal_preserves_input_bytes_and_has_exact_rc(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "caller.json"
            for slot in ROLES:
                p = request(slot)
                for bad in (False, True):
                    q = copy.deepcopy(p)
                    if bad:
                        q["extra_unpinned"] = True
                    content = core.canonical_json(q).encode()
                    path.write_bytes(content)
                    output = io.StringIO()
                    with redirect_stdout(output):
                        rc = contracts.main(["--slot", slot, "--kind", "request", "--in", str(path)])
                    self.assertEqual(rc, 2 if bad else 0)
                    parsed = json.loads(output.getvalue())
                    self.assertEqual(parsed["rc"], rc)
                    self.assertEqual(output.getvalue(), core.canonical_json(parsed))
                    self.assertEqual(path.read_bytes(), content)
            for content in (b'{"schema":1,"schema":2}', b'{"value":1.5}', b'{"value":NaN}'):
                path.write_bytes(content)
                with redirect_stdout(io.StringIO()):
                    rc = contracts.main(["--slot", "texture", "--kind", "request", "--in", str(path)])
                self.assertEqual(rc, 2)
                self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
