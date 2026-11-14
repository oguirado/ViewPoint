"""C-11 workflow unit promotion and Markdown roundtrip regressions for defect D-A8-13."""
from copy import deepcopy
import json
import unittest

import wender.remember.skills as skills
from wender.remember.skills import SkillRejected, admit


def make_valid_c11_skill(support_n=3, min_err=0, reproduced=True, verified_by="verifier"):
    return {
        "id": "skill_c11_workflow_unit_01",
        "intent": "Execute C-11 workflow unit procedure 🚀 测试",
        "params": {"timeout_ms": 1000, "retry_limit": 3},
        "precondition_signature": {
            "screen_fp": "fp_c11_pre_12345",
            "ax_stable_sha": "sha_c11_pre_67890"
        },
        "steps": [
            {"path": "api", "action": "initialize_context"},
            {"path": "ax", "action": "click_primary_action"}
        ],
        "checkpoints": ["action_dispatched", "response_received"],
        "expected_post": {
            "screen_fp": "fp_c11_post_12345",
            "ax_stable_sha": "sha_c11_post_67890"
        },
        "failure_recovery": {
            "fallback_step": {"path": "api", "action": "reset_state"}
        },
        "provenance": {
            "source": "workflow_unit_test_suite",
            "timestamp": "2026-10-23T20:00:00Z"
        },
        "evidence": {
            "reproduced": reproduced,
            "verified_by": verified_by,
            "support_n": support_n,
            "min_err": min_err,
            "version": 1
        }
    }


class TestC11WorkflowUnitPromotionAndMd(unittest.TestCase):
    def test_promotion_positive_control_support_3_min_err_0(self):
        skill = make_valid_c11_skill(support_n=3, min_err=0)
        admitted = admit(skill)
        self.assertIsInstance(admitted, str)
        parsed = json.loads(admitted)
        self.assertEqual(parsed["id"], "skill_c11_workflow_unit_01")
        self.assertEqual(parsed["evidence"]["support_n"], 3)
        self.assertEqual(parsed["evidence"]["min_err"], 0)

    def test_promotion_rejected_support_2_min_err_0(self):
        # Defect D-A8-13: absent C-11 promotion threshold enforcement in admit for support_n < 3
        skill = make_valid_c11_skill(support_n=2, min_err=0)
        with self.assertRaises(SkillRejected):
            admit(skill)

    def test_promotion_rejected_support_3_min_err_neg_1(self):
        # Defect D-A8-13: absent C-11 promotion threshold enforcement in admit for min_err < 0
        skill = make_valid_c11_skill(support_n=3, min_err=-1)
        with self.assertRaises(SkillRejected):
            admit(skill)

    def test_markdown_roundtrip_preserves_all_fields_and_unicode(self):
        # Defect D-A8-13: absent render_skill_md and parse_skill_md target functions in wender.remember.skills
        render_skill_md = getattr(skills, "render_skill_md", None)
        parse_skill_md = getattr(skills, "parse_skill_md", None)
        if render_skill_md is None or parse_skill_md is None:
            self.fail("Defect D-A8-13: render_skill_md or parse_skill_md is missing on wender.remember.skills")
        skill = make_valid_c11_skill(support_n=3, min_err=0)
        rendered_md = render_skill_md(skill)
        self.assertIsInstance(rendered_md, str)
        parsed = parse_skill_md(rendered_md)
        self.assertEqual(parsed, skill)
        self.assertEqual(render_skill_md(parsed), rendered_md)


def structured_unit():
    return {
        "id": "open", "intent": "Open → 設定", "params": {},
        "precondition_signature": {"screen": "home"},
        "steps": [{"path": "ax", "action": {"invoke": "settings"}}],
        "checkpoints": [{"after_step": 0, "expected_post": {"screen": "settings"}}],
        "expected_post": {"screen": "settings"},
        "failure_recovery": {"policy": "abort", "steps": []},
        "support_n": 3, "min_err": 0,
        "provenance": {"source": "offline:fixture"},
        "evidence": {"reproduced": True, "verified_by": "fixture/v1"},
    }


class TestProductionC11StrictInterchange(unittest.TestCase):
    def test_version_lifecycle_retains_evidence_and_caller_ownership(self):
        draft = structured_unit()
        draft["evidence"].update(runs=3, sha256="a" * 64)
        original = deepcopy(draft)
        first = skills.promote_skill(draft)
        second = skills.promote_skill(draft, previous=first)
        self.assertEqual(first["evidence"]["version"], 1)
        self.assertEqual(second["evidence"]["version"], 2)
        self.assertEqual(second["evidence"]["runs"], 3)
        self.assertEqual(second["evidence"]["sha256"], "a" * 64)
        self.assertEqual(draft, original)
        second["params"]["modified"] = True
        self.assertEqual(first["params"], {})
        self.assertEqual(draft, original)

    def test_promotion_truth_table_for_both_supported_counter_locations(self):
        for constructor in (structured_unit, make_valid_c11_skill):
            for support, error, eligible in ((2, 0, False), (3, -1, False),
                                             (3, 0, True), (4, 1, True)):
                unit = constructor()
                counters = unit if "support_n" in unit else unit["evidence"]
                counters.update(support_n=support, min_err=error)
                self.assertIs(skills.promotion_eligible(unit), eligible)
                unit["evidence"]["reproduced"] = False
                self.assertIs(skills.promotion_eligible(unit), False)
                unit["evidence"].update(reproduced=True, verified_by="   ")
                self.assertIs(skills.promotion_eligible(unit), False)

    def test_boolean_counts_and_malformed_versions_are_not_integers(self):
        for field in ("support_n", "min_err"):
            for value in (True, False, "3", 3.0):
                unit = structured_unit()
                unit[field] = value
                with self.assertRaises(SkillRejected):
                    skills.promote_skill(unit)
        for version in (True, False, "1", 0, -1, None, 1.0, 2):
            unit = structured_unit()
            unit["evidence"]["version"] = version
            with self.assertRaises(SkillRejected):
                skills.promote_skill(unit)

    def test_previous_invalid_or_different_id_rejected_without_mutation(self):
        first = skills.promote_skill(structured_unit())
        for version in (None, True, 0, -1, "1"):
            previous = deepcopy(first)
            previous["evidence"]["version"] = version
            before = deepcopy(previous)
            with self.assertRaises(SkillRejected):
                skills.promote_skill(structured_unit(), previous=previous)
            self.assertEqual(previous, before)
        previous = deepcopy(first)
        del previous["evidence"]["version"]
        with self.assertRaises(SkillRejected):
            skills.promote_skill(structured_unit(), previous=previous)
        previous = deepcopy(first)
        previous["id"] = "different"
        with self.assertRaises(SkillRejected):
            skills.promote_skill(structured_unit(), previous=previous)

    def test_structured_schema_rejects_unknown_fields_and_bad_checkpoints(self):
        for mutate in (
                lambda unit: unit.update(extra="not-in-closed-schema"),
                lambda unit: unit["checkpoints"][0].update(after_step=True),
                lambda unit: unit["checkpoints"][0].update(after_step=1),
                lambda unit: unit["steps"][0].update(action="unstructured"),
                lambda unit: unit["failure_recovery"].update(policy="unknown"),
                lambda unit: unit["provenance"].update(extra="unknown")):
            unit = structured_unit()
            mutate(unit)
            with self.assertRaises(SkillRejected):
                skills.promote_skill(unit)

    def test_exact_canonical_markdown_and_unicode(self):
        unit = skills.promote_skill(structured_unit())
        expected = (
            '# Wender C-11 skill unit (v1)\n\n```json\n'
            '{"checkpoints":[{"after_step":0,"expected_post":{"screen":"settings"}}],'
            '"evidence":{"reproduced":true,"verified_by":"fixture/v1","version":1},'
            '"expected_post":{"screen":"settings"},'
            '"failure_recovery":{"policy":"abort","steps":[]},'
            '"id":"open","intent":"Open → 設定","min_err":0,"params":{},'
            '"precondition_signature":{"screen":"home"},'
            '"provenance":{"source":"offline:fixture"},'
            '"steps":[{"action":{"invoke":"settings"},"path":"ax"}],"support_n":3}'
            '\n```\n')
        self.assertEqual(skills.render_skill_md(unit), expected)
        self.assertEqual(skills.parse_skill_md(expected), unit)

    def test_markdown_rejects_duplicates_floats_noncanonical_and_envelope_changes(self):
        markdown = skills.render_skill_md(skills.promote_skill(structured_unit()))
        for invalid in (
                markdown.replace('"version":1', '"version":1,"version":2'),
                markdown.replace('"version":1', '"version":1.0'),
                markdown.replace('"version":1', '"version":NaN'),
                markdown.replace('"version":1', '"version": 1'),
                markdown.replace('→', '\\u2192'),
                markdown.replace('\n', '\r\n'),
                markdown[:-1], markdown + 'extra',
                markdown.replace('```json', '```JSON')):
            with self.subTest(markdown=invalid):
                with self.assertRaises(SkillRejected):
                    skills.parse_skill_md(invalid)


if __name__ == "__main__":
    unittest.main()
