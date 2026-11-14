"""Unit promotion and Markdown roundtrip tests for wender.remember.skills (C-11)."""
import json
import unittest

import wender.remember.skills as skills
from wender.remember.skills import SkillRejected, admit


def make_valid_c11_skill(support_n=3, min_err=0, reproduced=True, verified_by="verifier", intent="Execute C-11 skill procedure"):
    return {
        "id": "skill_c11_promotion_md_01",
        "intent": intent,
        "params": {"timeout_ms": 1000},
        "precondition_signature": {
            "screen": "home",
            "ax_stable_sha": "sha_pre_123"
        },
        "steps": [
            {"path": "ax", "action": {"invoke": "settings"}}
        ],
        "checkpoints": [
            {"after_step": 0, "expected_post": {"screen": "settings"}}
        ],
        "expected_post": {
            "screen": "settings"
        },
        "failure_recovery": {
            "policy": "abort",
            "steps": []
        },
        "provenance": {
            "source": "promotion_md_test_suite"
        },
        "evidence": {
            "reproduced": reproduced,
            "verified_by": verified_by,
            "support_n": support_n,
            "min_err": min_err,
            "version": 1
        }
    }


class TestFx2F4J2C11SkillUnitPromotionAndMarkdown(unittest.TestCase):
    # -------------------------------------------------------------------------
    # Ordinary Passing Controls (current skills.admit behavior)
    # -------------------------------------------------------------------------

    def test_admit_valid_c11_skill_positive_control(self):
        skill = make_valid_c11_skill(support_n=3, min_err=0)
        result = admit(skill)
        self.assertIsInstance(result, str)
        decoded = json.loads(result)
        self.assertEqual(decoded["id"], "skill_c11_promotion_md_01")
        self.assertEqual(decoded["evidence"]["support_n"], 3)
        self.assertEqual(decoded["evidence"]["min_err"], 0)

    def test_admit_high_valid_min_err_control(self):
        skill = make_valid_c11_skill(support_n=5, min_err=100)
        result = admit(skill)
        self.assertIsInstance(result, str)
        decoded = json.loads(result)
        self.assertEqual(decoded["evidence"]["support_n"], 5)
        self.assertEqual(decoded["evidence"]["min_err"], 100)

    def test_admit_unicode_preservation_without_normalization_control(self):
        unnormalized_intent = "Abrir ajustes — 設定 🚀 e\u0301"
        skill = make_valid_c11_skill(intent=unnormalized_intent)
        result = admit(skill)
        decoded = json.loads(result)
        self.assertEqual(decoded["intent"], unnormalized_intent)
        self.assertIn("e\u0301", decoded["intent"])

    def test_admit_legacy_missing_required_field(self):
        skill = make_valid_c11_skill()
        del skill["intent"]
        with self.assertRaises(SkillRejected) as cm:
            admit(skill)
        self.assertEqual(str(cm.exception), "missing ['intent']")

    def test_admit_legacy_empty_steps(self):
        skill = make_valid_c11_skill()
        skill["steps"] = []
        with self.assertRaises(SkillRejected) as cm:
            admit(skill)
        self.assertEqual(str(cm.exception), "no steps")

    def test_admit_legacy_empty_checkpoints(self):
        skill = make_valid_c11_skill()
        skill["checkpoints"] = []
        with self.assertRaises(SkillRejected) as cm:
            admit(skill)
        self.assertEqual(str(cm.exception), "no verification checkpoints")

    def test_admit_legacy_reproduction_not_verified(self):
        skill = make_valid_c11_skill(reproduced=False)
        with self.assertRaises(SkillRejected) as cm:
            admit(skill)
        self.assertEqual(
            str(cm.exception),
            "reproduction not verified (evidence.reproduced/verified_by)"
        )

    def test_admit_legacy_missing_provenance_source(self):
        skill = make_valid_c11_skill()
        skill["provenance"] = {}
        with self.assertRaises(SkillRejected) as cm:
            admit(skill)
        self.assertEqual(str(cm.exception), "provenance.source required")

    def test_admit_legacy_invalid_step_path(self):
        skill = make_valid_c11_skill()
        skill["steps"] = [{"path": "invalid_path", "action": {}}]
        with self.assertRaises(SkillRejected) as cm:
            admit(skill)
        self.assertEqual(str(cm.exception), "step path must be api|ax|coordinates")

    # -------------------------------------------------------------------------
    # Demonstrated Missing Behavior: C-11 Promotion Threshold Boundaries
    # -------------------------------------------------------------------------

    def test_admit_rejects_support_n_below_3(self):
        # Defect: absent C-11 support_n >= 3 promotion check in skills.admit
        skill = make_valid_c11_skill(support_n=2, min_err=0)
        with self.assertRaises(SkillRejected):
            admit(skill)

    def test_admit_rejects_negative_min_err(self):
        # Defect: absent C-11 min_err >= 0 promotion check in skills.admit
        skill = make_valid_c11_skill(support_n=3, min_err=-1)
        with self.assertRaises(SkillRejected):
            admit(skill)

    def test_admit_rejects_boolean_impostor_support_n(self):
        # Defect: absent C-11 type check rejecting boolean impostor for support_n
        skill = make_valid_c11_skill(support_n=True, min_err=0)
        with self.assertRaises(SkillRejected):
            admit(skill)

    def test_admit_rejects_boolean_impostor_min_err(self):
        # Defect: absent C-11 type check rejecting boolean impostor for min_err
        skill = make_valid_c11_skill(support_n=3, min_err=False)
        with self.assertRaises(SkillRejected):
            admit(skill)

    def test_admit_rejects_string_impostor_support_n(self):
        # Defect: absent C-11 type check rejecting string impostor for support_n
        skill = make_valid_c11_skill(support_n="3", min_err=0)
        with self.assertRaises(SkillRejected):
            admit(skill)

    def test_admit_rejects_string_impostor_min_err(self):
        # Defect: absent C-11 type check rejecting string impostor for min_err
        skill = make_valid_c11_skill(support_n=3, min_err="0")
        with self.assertRaises(SkillRejected):
            admit(skill)

    # -------------------------------------------------------------------------
    # Demonstrated Missing Behavior: Markdown Rendering & Parsing
    # -------------------------------------------------------------------------

    def test_render_and_parse_skill_md_roundtrip(self):
        # Defect: absent render_skill_md and parse_skill_md in wender.remember.skills
        render_skill_md = getattr(skills, "render_skill_md", None)
        parse_skill_md = getattr(skills, "parse_skill_md", None)
        if render_skill_md is None or parse_skill_md is None:
            self.fail("render_skill_md or parse_skill_md missing on wender.remember.skills")
        skill = make_valid_c11_skill(support_n=3, min_err=0)
        rendered_md = render_skill_md(skill)
        self.assertIsInstance(rendered_md, (str, bytes))
        parsed = parse_skill_md(rendered_md)
        self.assertEqual(parsed, skill)

    def test_parse_skill_md_literal_target_v1_valid(self):
        # Defect: absent parse_skill_md in wender.remember.skills
        parse_skill_md = getattr(skills, "parse_skill_md", None)
        if parse_skill_md is None:
            self.fail("parse_skill_md missing on wender.remember.skills")

        literal_md = (
            "# Wender C-11 skill unit (v1)\n\n"
            "```json\n"
            '{"checkpoints":[{"after_step":0,"expected_post":{"screen":"settings"}}],'
            '"evidence":{"min_err":0,"reproduced":true,"support_n":3,"verified_by":"verifier","version":1},'
            '"expected_post":{"screen":"settings"},"failure_recovery":{"policy":"abort","steps":[]},'
            '"id":"skill_c11_promotion_md_01","intent":"Execute C-11 skill procedure","params":{"timeout_ms":1000},'
            '"precondition_signature":{"ax_stable_sha":"sha_pre_123","screen":"home"},'
            '"provenance":{"source":"promotion_md_test_suite"},'
            '"steps":[{"action":{"invoke":"settings"},"path":"ax"}]}\n'
            "```\n"
        )
        parsed = parse_skill_md(literal_md)
        self.assertEqual(parsed["id"], "skill_c11_promotion_md_01")
        self.assertEqual(parsed["evidence"]["support_n"], 3)
        self.assertEqual(parsed["evidence"]["min_err"], 0)

    def test_parse_skill_md_rejects_nested_duplicate_members(self):
        # Defect: absent parse_skill_md in wender.remember.skills
        parse_skill_md = getattr(skills, "parse_skill_md", None)
        if parse_skill_md is None:
            self.fail("parse_skill_md missing on wender.remember.skills")

        # JSON containing duplicate key "timeout_ms" inside nested "params"
        literal_duplicate_md = (
            "# Wender C-11 skill unit (v1)\n\n"
            "```json\n"
            '{"checkpoints":[{"after_step":0,"expected_post":{"screen":"settings"}}],'
            '"evidence":{"min_err":0,"reproduced":true,"support_n":3,"verified_by":"verifier","version":1},'
            '"expected_post":{"screen":"settings"},"failure_recovery":{"policy":"abort","steps":[]},'
            '"id":"skill_c11_promotion_md_01","intent":"Execute C-11 skill procedure",'
            '"params":{"timeout_ms":1000,"timeout_ms":2000},'
            '"precondition_signature":{"ax_stable_sha":"sha_pre_123","screen":"home"},'
            '"provenance":{"source":"promotion_md_test_suite"},'
            '"steps":[{"action":{"invoke":"settings"},"path":"ax"}]}\n'
            "```\n"
        )
        with self.assertRaises((ValueError, SkillRejected)):
            parse_skill_md(literal_duplicate_md)

    def test_parse_skill_md_rejects_noncanonical_json(self):
        # Defect: absent parse_skill_md in wender.remember.skills
        parse_skill_md = getattr(skills, "parse_skill_md", None)
        if parse_skill_md is None:
            self.fail("parse_skill_md missing on wender.remember.skills")

        # Formatted JSON with whitespace and newlines, or floating point "min_err": 0.0
        literal_noncanonical_md = (
            "# Wender C-11 skill unit (v1)\n\n"
            "```json\n"
            "{\n"
            '  "id": "skill_c11_promotion_md_01",\n'
            '  "evidence": {"min_err": 0.0}\n'
            "}\n"
            "```\n"
        )
        with self.assertRaises((ValueError, SkillRejected)):
            parse_skill_md(literal_noncanonical_md)

    def test_parse_skill_md_rejects_crlf(self):
        # Defect: absent parse_skill_md in wender.remember.skills
        parse_skill_md = getattr(skills, "parse_skill_md", None)
        if parse_skill_md is None:
            self.fail("parse_skill_md missing on wender.remember.skills")

        literal_crlf_md = (
            "# Wender C-11 skill unit (v1)\r\n\r\n"
            "```json\r\n"
            '{"checkpoints":[{"after_step":0,"expected_post":{"screen":"settings"}}],'
            '"evidence":{"min_err":0,"reproduced":true,"support_n":3,"verified_by":"verifier","version":1},'
            '"expected_post":{"screen":"settings"},"failure_recovery":{"policy":"abort","steps":[]},'
            '"id":"skill_c11_promotion_md_01","intent":"Execute C-11 skill procedure","params":{"timeout_ms":1000},'
            '"precondition_signature":{"ax_stable_sha":"sha_pre_123","screen":"home"},'
            '"provenance":{"source":"promotion_md_test_suite"},'
            '"steps":[{"action":{"invoke":"settings"},"path":"ax"}]}\r\n'
            "```\r\n"
        )
        with self.assertRaises((ValueError, SkillRejected)):
            parse_skill_md(literal_crlf_md)

    def test_parse_skill_md_rejects_malformed_fences(self):
        # Defect: absent parse_skill_md in wender.remember.skills
        parse_skill_md = getattr(skills, "parse_skill_md", None)
        if parse_skill_md is None:
            self.fail("parse_skill_md missing on wender.remember.skills")

        literal_bad_fence_md = (
            "# Wender C-11 skill unit (v1)\n\n"
            "```text\n"
            '{"id":"skill_c11_promotion_md_01"}\n'
            "```\n"
        )
        with self.assertRaises((ValueError, SkillRejected)):
            parse_skill_md(literal_bad_fence_md)

    def test_parse_skill_md_rejects_truncation(self):
        # Defect: absent parse_skill_md in wender.remember.skills
        parse_skill_md = getattr(skills, "parse_skill_md", None)
        if parse_skill_md is None:
            self.fail("parse_skill_md missing on wender.remember.skills")

        literal_truncated_md = (
            "# Wender C-11 skill unit (v1)\n\n"
            "```json\n"
            '{"id":"skill_c11_promotion_md_01"'
        )
        with self.assertRaises((ValueError, SkillRejected)):
            parse_skill_md(literal_truncated_md)

    def test_parse_skill_md_rejects_trailing_material(self):
        # Defect: absent parse_skill_md in wender.remember.skills
        parse_skill_md = getattr(skills, "parse_skill_md", None)
        if parse_skill_md is None:
            self.fail("parse_skill_md missing on wender.remember.skills")

        literal_trailing_md = (
            "# Wender C-11 skill unit (v1)\n\n"
            "```json\n"
            '{"checkpoints":[{"after_step":0,"expected_post":{"screen":"settings"}}],'
            '"evidence":{"min_err":0,"reproduced":true,"support_n":3,"verified_by":"verifier","version":1},'
            '"expected_post":{"screen":"settings"},"failure_recovery":{"policy":"abort","steps":[]},'
            '"id":"skill_c11_promotion_md_01","intent":"Execute C-11 skill procedure","params":{"timeout_ms":1000},'
            '"precondition_signature":{"ax_stable_sha":"sha_pre_123","screen":"home"},'
            '"provenance":{"source":"promotion_md_test_suite"},'
            '"steps":[{"action":{"invoke":"settings"},"path":"ax"}]}\n'
            "```\n"
            "Trailing material after fence\n"
        )
        with self.assertRaises((ValueError, SkillRejected)):
            parse_skill_md(literal_trailing_md)

    def test_parse_skill_md_preserves_unicode_without_normalization(self):
        # Defect: absent parse_skill_md in wender.remember.skills
        parse_skill_md = getattr(skills, "parse_skill_md", None)
        if parse_skill_md is None:
            self.fail("parse_skill_md missing on wender.remember.skills")

        unnormalized_intent = "Abrir ajustes — 設定 🚀 e\u0301"
        literal_unicode_md = (
            "# Wender C-11 skill unit (v1)\n\n"
            "```json\n"
            '{"checkpoints":[{"after_step":0,"expected_post":{"screen":"settings"}}],'
            '"evidence":{"min_err":0,"reproduced":true,"support_n":3,"verified_by":"verifier","version":1},'
            '"expected_post":{"screen":"settings"},"failure_recovery":{"policy":"abort","steps":[]},'
            '"id":"skill_c11_promotion_md_01","intent":"' + unnormalized_intent + '","params":{"timeout_ms":1000},'
            '"precondition_signature":{"ax_stable_sha":"sha_pre_123","screen":"home"},'
            '"provenance":{"source":"promotion_md_test_suite"},'
            '"steps":[{"action":{"invoke":"settings"},"path":"ax"}]}\n'
            "```\n"
        )
        parsed = parse_skill_md(literal_unicode_md)
        self.assertEqual(parsed["intent"], unnormalized_intent)
        self.assertIn("e\u0301", parsed["intent"])


if __name__ == "__main__":
    unittest.main()
