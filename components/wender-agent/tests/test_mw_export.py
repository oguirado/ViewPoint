"""Unit tests for wender.learn.mw_export (B3 wender skill renderer)."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from tools import memoryweb_gate as gate
from wender.learn import mw_export
from wender.remember import skills
from wender.remember.skills import SkillRejected

ROOT = Path(__file__).resolve().parents[1]
PINS_FIXTURE_PATH = ROOT / "tests" / "fixtures" / "mw_schema_pins.json"


def admitted_c11_skill_fixture():
    """Return a promoted C-11 skill exercising click, key, type, wait, and done."""
    draft = {
        "id": "skill.checkout.guest",
        "intent": "Complete guest checkout by clicking coupon input, typing code, pressing Enter, waiting, and finishing",
        "params": {"coupon_slot": "slot-2"},
        "steps": [
            {
                "path": "coordinates",
                "action": {
                    "kind": "click",
                    "x": 420,
                    "y": 310,
                    "expected_change_box_px": [380, 280, 620, 360],
                    "mark": {
                        "mark_id": "m-coupon",
                        "kind": "input",
                        "box_px": [390, 290, 600, 340],
                        "relative_xy_bps": [1428, 4000],
                        "ocr_text": "Coupon code",
                        "ocr_score_promille": 940,
                        "missing_reason": None,
                    },
                },
            },
            {
                "path": "ax",
                "action": {
                    "kind": "type",
                    "text": "SECRET99",
                },
            },
            {
                "path": "ax",
                "action": {
                    "kind": "key",
                    "keysym": "Return",
                },
            },
            {
                "path": "coordinates",
                "action": {
                    "kind": "wait",
                    "ms": 450,
                },
            },
            {
                "path": "api",
                "action": {
                    "kind": "done",
                },
            },
        ],
        "checkpoints": [
            {"after_step": 0, "expected_post": {"focused": "coupon"}},
            {"after_step": 2, "expected_post": {"coupon_applied": True}},
            {"after_step": 4, "expected_post": {"order_submitted": True}},
        ],
        "provenance": {"source": "fixture://checkout-guest"},
        "evidence": {
            "reproduced": True,
            "verified_by": "replay-harness-v1",
            "runs": 4,
            "sha256": "a1b2c3d4" * 8,
        },
        "precondition_signature": {"page": "checkout", "cart_nonempty": True},
        "expected_post": {"order_submitted": True},
        "failure_recovery": {"policy": "abort", "steps": []},
        "support_n": 4,
        "min_err": 0,
    }
    return skills.promote_skill(draft)


class SchemaPinTests(unittest.TestCase):
    def test_schema_pins_fixture_matches_measured_digests(self):
        self.assertTrue(PINS_FIXTURE_PATH.is_file())
        fixture_pins = json.loads(PINS_FIXTURE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(fixture_pins, mw_export.SCHEMA_PINS)
        computed = mw_export.verify_schema_pins()
        self.assertEqual(computed, fixture_pins)

    def test_cu_action_schema_enum_matches_vocabulary_constant(self):
        cu_schema = json.loads(mw_export.CU_ACTION_SCHEMA_PATH.read_text(encoding="utf-8"))
        enum_kinds = tuple(cu_schema["properties"]["action"]["properties"]["kind"]["enum"])
        self.assertEqual(enum_kinds, ("click", "key", "type", "wait", "done"))
        self.assertEqual(enum_kinds, mw_export.CU_ACTION_VOCABULARY)


class VocabularyMappingTests(unittest.TestCase):
    def test_all_five_cu_action_kinds_map_to_demo_and_workflow_kinds(self):
        expected_demo = {
            "click": "click",
            "key": "key",
            "type": "paste",
            "wait": "wait",
            "done": "wait",
        }
        expected_wf = {
            "click": "click",
            "key": "key",
            "type": "paste",
            "wait": "wait_signal",
            "done": "wait_signal",
        }
        for kind in mw_export.CU_ACTION_VOCABULARY:
            self.assertEqual(mw_export.map_cu_action_kind(kind), expected_demo[kind])
            self.assertEqual(mw_export.map_workflow_action_kind(kind), expected_wf[kind])

    def test_cu_action_envelope_maps_to_demo_step_and_redacts_raw_text(self):
        cu_type = {
            "schema": "cu.action/v1",
            "observation": "coupon field focused",
            "action": {
                "kind": "type",
                "x": None,
                "y": None,
                "keysym": None,
                "text": "PRIVATE_CODE_123",
                "ms": None,
                "element_id": "coupon-input",
                "mark_id": None,
            },
            "expectation": {
                "kind": "changed_region",
                "region": {"x": 100, "y": 200, "w": 150, "h": 40},
            },
            "confidence": 1,
            "need_reasoning": None,
        }
        dstep = mw_export.map_cu_action_to_demo_step(cu_type, seq=2)
        self.assertEqual(dstep["seq"], 2)
        self.assertEqual(dstep["kind"], "paste")
        self.assertEqual(dstep["text_len"], len("PRIVATE_CODE_123"))
        self.assertEqual(dstep["expected_change_box_px"], [100, 200, 250, 240])
        self.assertNotIn("PRIVATE_CODE_123", json.dumps(dstep))

    def test_invalid_action_kinds_and_bad_action_fields_are_refused(self):
        bad_actions = [
            {"kind": "hover", "x": 10, "y": 20},
            {"kind": 123},
            {"kind": "key"},
            {"kind": "key", "keysym": "   "},
            {"kind": "type"},
            {"kind": "type", "text": ""},
            {"kind": "wait", "ms": -5},
            {"kind": "done", "text": "unexpected"},
            {"kind": "done", "keysym": "Escape"},
            {"kind": "click", "x": 10},
            {"kind": "click", "x": 10, "y": 20, "button": 4},
        ]
        for bad in bad_actions:
            with self.subTest(bad=bad):
                with self.assertRaises(mw_export.MwExportError):
                    mw_export.map_cu_action_to_demo_step(bad, seq=1)


class AdmissionRefusalTests(unittest.TestCase):
    def test_unverified_and_non_c11_skills_are_refused(self):
        base = admitted_c11_skill_fixture()

        unreproduced = deepcopy(base)
        unreproduced["evidence"]["reproduced"] = False

        blank_verifier = deepcopy(base)
        blank_verifier["evidence"]["verified_by"] = "   "

        low_support = deepcopy(base)
        low_support["support_n"] = 2

        negative_err = deepcopy(base)
        negative_err["min_err"] = -1

        missing_version = deepcopy(base)
        del missing_version["evidence"]["version"]

        legacy_only = {
            "id": "legacy.skill",
            "intent": "Legacy non-C11 skill",
            "params": {},
            "steps": [{"path": "api", "action": {"name": "open"}}],
            "checkpoints": [{"after_step": 0, "expected_post": {"ok": True}}],
            "provenance": {"source": "fixture://legacy"},
            "evidence": {"reproduced": True, "verified_by": "verifier"},
        }

        pixel_key = deepcopy(base)
        pixel_key["params"]["screenshot"] = "forbidden"

        float_val = deepcopy(base)
        float_val["params"]["ratio"] = 0.5

        for case in (
            unreproduced,
            blank_verifier,
            low_support,
            negative_err,
            missing_version,
            legacy_only,
            pixel_key,
            float_val,
            "not a markdown envelope",
            123,
        ):
            for fn in (
                mw_export.admit_c11_skill,
                mw_export.render_demo_steps,
                mw_export.render_workflow_steps,
                mw_export.render_explanation,
                mw_export.render_workflow_draft,
                mw_export.render_run_bundle,
                mw_export.export_skill_draft,
            ):
                with self.subTest(fn=fn.__name__, case=repr(case)[:60]):
                    with self.assertRaises(SkillRejected):
                        fn(case)


class ExportDraftTests(unittest.TestCase):
    def test_export_skill_draft_builds_valid_workflow_demo_steps_explanation_and_bundle(self):
        skill = admitted_c11_skill_fixture()
        out = mw_export.export_skill_draft(
            skill,
            site_id="acme-checkout",
            site_name="Acme Checkout Portal",
            domains=("checkout.example.invalid",),
            recorded_date="2026-10-28",
            tags=("checkout", "click", "type"),
        )

        # 1. demo_steps check
        demo_steps = out["demo_steps"]
        self.assertEqual(demo_steps["schema"], "mw.demo_steps.v1")
        self.assertEqual(demo_steps["site_id"], "acme-checkout")
        self.assertEqual([s["kind"] for s in demo_steps["steps"]], ["click", "paste", "key", "wait", "wait"])
        self.assertEqual(demo_steps["steps"][1]["text_len"], 8)
        self.assertEqual(demo_steps["steps"][2]["key_name"], "Return")
        self.assertEqual(demo_steps["steps"][3]["gap_after_ms"], 450)
        self.assertEqual(demo_steps["steps"][4]["gap_after_ms"], 0)
        self.assertNotIn("SECRET99", json.dumps(out, default=str))

        # 2. workflow and steps check
        wf = out["workflow"]
        self.assertEqual(wf["schema"], "mw.workflow.v1")
        self.assertEqual(wf["status"], "draft")
        self.assertEqual(wf["site_id"], "acme-checkout")
        self.assertEqual(wf["id"], "skill-checkout-guest")
        self.assertEqual([s["action"]["kind"] for s in wf["steps"]], [
            "click", "paste", "key", "wait_signal", "wait_signal"
        ])
        self.assertEqual([s["action"]["path"] for s in wf["steps"]], [
            "coordinates", "ax", "ax", "coordinates", "api"
        ])
        self.assertEqual(len(wf["text_slots"]), 1)
        self.assertEqual(wf["text_slots"][0]["slot_id"], "slot-2")
        self.assertEqual(wf["text_slots"][0]["max_length"], 8)
        self.assertEqual(wf["steps"][1]["action"]["text_slot"], "slot-2")
        self.assertEqual(wf["stats"]["support_n"], 4)
        self.assertEqual(wf["stats"]["reproduced_ppm"], 1000000)

        defs = gate.compile_bundle(gate.strict_load(mw_export.MW_V1_SCHEMA_PATH.read_bytes()))
        self.assertTrue(gate.conforms(wf, defs["workflow"], defs))
        self.assertTrue(gate.conforms(demo_steps, defs["demo_steps"], defs))

        # 3. six-heading EXPLANATION check
        expl = out["explanation"]
        expl_bytes = out["explanation_bytes"]
        headings = [line[3:] for line in expl.splitlines() if line.startswith("## ")]
        self.assertEqual(tuple(headings), mw_export.EXPLANATION_HEADINGS)
        self.assertIn("step 2 length 8", expl)
        self.assertNotIn("SECRET99", expl)
        span = gate.extract_span(expl_bytes, "intent")
        self.assertIn(b"## Intent", span)
        with tempfile.TemporaryDirectory() as tmp:
            expl_path = Path(tmp) / "EXPLANATION.md"
            expl_path.write_bytes(expl_bytes)
            res = gate.check_explanation(expl_path)
            self.assertEqual(res, gate.GateResult(0, "PASS"))

        # 4. run_bundle check
        rb = out["run_bundle"]
        self.assertEqual(rb["schema"], "mw.run_bundle.v1")
        self.assertEqual(rb["receipt_sha256"], "a1b2c3d4" * 8)
        self.assertEqual(len(rb["steps"]), 5)
        rb_defs = gate.compile_bundle(mw_export.RUN_BUNDLE_SCHEMA)
        self.assertTrue(gate.conforms(rb, rb_defs["run_bundle"], rb_defs))

    def test_accepts_canonical_c11_markdown_envelope_and_minimal_c11_record(self):
        skill = admitted_c11_skill_fixture()
        md = skills.render_skill_md(skill)
        from_dict = mw_export.export_skill_draft(skill, site_id="acme-checkout")
        from_md = mw_export.export_skill_draft(md, site_id="acme-checkout")
        self.assertEqual(from_dict, from_md)

        minimal = skills.promote_skill({
            "id": "skill.open",
            "intent": "Open a panel",
            "params": {"name": "demo"},
            "steps": [{"path": "api", "action": {"name": "open"}}],
            "checkpoints": [{"after_step": 0, "expected_post": {"screen": "ready"}}],
            "provenance": {"source": "fixture://demo"},
            "evidence": {"reproduced": True, "verified_by": "fixture-verifier"},
            "precondition_signature": {"screen": "idle"},
            "expected_post": {"screen": "ready"},
            "failure_recovery": {"policy": "abort", "steps": []},
            "support_n": 3,
            "min_err": 0,
        })
        minimal_out = mw_export.export_skill_draft(minimal)
        self.assertEqual(minimal_out["workflow"]["id"], "skill-open")
        self.assertEqual(minimal_out["workflow"]["steps"][0]["action"]["path"], "api")
        self.assertEqual(minimal_out["workflow"]["steps"][0]["action"]["kind"], "click")


if __name__ == "__main__":
    unittest.main()
