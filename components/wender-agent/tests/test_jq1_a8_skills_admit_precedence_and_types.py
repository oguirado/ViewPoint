"""Admission precedence and malformed container type tests for skills.admit (D-A8-09)."""
import json
import unittest

from wender.remember import skills

VALID_SKILL = {
    "id": "skill-01",
    "intent": "test skill admission",
    "params": {"key": "val"},
    "steps": [{"path": "ax", "action": "click"}],
    "checkpoints": [{"after": 0, "expect": "ok"}],
    "provenance": {"source": "demo:01"},
    "evidence": {"reproduced": True, "verified_by": "verifier-01"},
}


class TestSkillAdmitPrecedenceAndTypes(unittest.TestCase):
    def test_legacy_verifier_source_and_version_metadata_preserved(self):
        # The old contract checks source/verifier truthiness. Store owns legacy
        # evidence.version merge refusals; new C-11 APIs enforce typed versions.
        skill = dict(VALID_SKILL, provenance={"source": {"demo": 1}},
                     evidence={"reproduced": True, "verified_by": ["verifier"],
                               "version": True})
        self.assertEqual(json.loads(skills.admit(skill)), skill)

    def test_recursive_malformed_metadata_is_skill_rejected(self):
        cycle = []
        cycle.append(cycle)
        for invalid in ({1.25: "key"}, {"nested": [{1: "key"}]},
                        {"n": 1.0}, {"binary": b"raw"}, {"screenshot": "encoded"},
                        {"cycle": cycle}, {"text": "\ud800"}):
            with self.subTest(value=repr(invalid)):
                with self.assertRaises(skills.SkillRejected):
                    skills.admit(dict(VALID_SKILL, params=invalid))

    def test_admit_valid_skill(self):
        result = skills.admit(VALID_SKILL)
        self.assertIsInstance(result, str)
        decoded = json.loads(result)
        self.assertEqual(decoded["id"], "skill-01")

    def test_precedence_order_1_missing_required_fields(self):
        # Order 1 (missing fields) precedes all lower checks (no steps, no checkpoints, bad evidence, bad provenance, bad step path)
        bad_skill = {
            "id": "skill-01",
            "params": {"key": "val"},
            "steps": [],
            "checkpoints": [],
            "provenance": {},
            "evidence": {"reproduced": False},
        }
        with self.assertRaises(skills.SkillRejected) as cm:
            skills.admit(bad_skill)
        self.assertEqual(str(cm.exception), "missing ['intent']")

    def test_precedence_order_2_no_steps(self):
        # Order 2 (no steps) precedes no checkpoints, bad evidence, bad provenance
        bad_skill = {
            "id": "skill-01",
            "intent": "test skill admission",
            "params": {"key": "val"},
            "steps": [],
            "checkpoints": [],
            "provenance": {},
            "evidence": {"reproduced": False},
        }
        with self.assertRaises(skills.SkillRejected) as cm:
            skills.admit(bad_skill)
        self.assertEqual(str(cm.exception), "no steps")

    def test_precedence_order_3_no_verification_checkpoints(self):
        # Order 3 (no checkpoints) precedes bad evidence, bad provenance, bad step path
        bad_skill = {
            "id": "skill-01",
            "intent": "test skill admission",
            "params": {"key": "val"},
            "steps": [{"path": "invalid_path"}],
            "checkpoints": [],
            "provenance": {},
            "evidence": {"reproduced": False},
        }
        with self.assertRaises(skills.SkillRejected) as cm:
            skills.admit(bad_skill)
        self.assertEqual(str(cm.exception), "no verification checkpoints")

    def test_precedence_order_4_reproduction_not_verified(self):
        # Order 4 (evidence check) precedes bad provenance and bad step path
        bad_skill = {
            "id": "skill-01",
            "intent": "test skill admission",
            "params": {"key": "val"},
            "steps": [{"path": "invalid_path"}],
            "checkpoints": [{"after": 0, "expect": "ok"}],
            "provenance": {},
            "evidence": {"reproduced": False},
        }
        with self.assertRaises(skills.SkillRejected) as cm:
            skills.admit(bad_skill)
        self.assertEqual(
            str(cm.exception),
            "reproduction not verified (evidence.reproduced/verified_by)",
        )

    def test_precedence_order_5_provenance_source_required(self):
        # Order 5 (provenance check) precedes bad step path
        bad_skill = {
            "id": "skill-01",
            "intent": "test skill admission",
            "params": {"key": "val"},
            "steps": [{"path": "invalid_path"}],
            "checkpoints": [{"after": 0, "expect": "ok"}],
            "provenance": {},
            "evidence": {"reproduced": True, "verified_by": "verifier-01"},
        }
        with self.assertRaises(skills.SkillRejected) as cm:
            skills.admit(bad_skill)
        self.assertEqual(str(cm.exception), "provenance.source required")

    def test_precedence_order_6_invalid_step_path(self):
        # Order 6 (step path check)
        bad_skill = {
            "id": "skill-01",
            "intent": "test skill admission",
            "params": {"key": "val"},
            "steps": [{"path": "invalid_path"}],
            "checkpoints": [{"after": 0, "expect": "ok"}],
            "provenance": {"source": "demo:01"},
            "evidence": {"reproduced": True, "verified_by": "verifier-01"},
        }
        with self.assertRaises(skills.SkillRejected) as cm:
            skills.admit(bad_skill)
        self.assertEqual(str(cm.exception), "step path must be api|ax|coordinates")

    def test_missing_required_ordering_intent_and_evidence(self):
        # Removing both intent and evidence checks REQUIRED ordering
        bad_skill = {
            "id": "skill-01",
            "params": {"key": "val"},
            "steps": [{"path": "ax", "action": "click"}],
            "checkpoints": [{"after": 0, "expect": "ok"}],
            "provenance": {"source": "demo:01"},
        }
        with self.assertRaises(skills.SkillRejected) as cm:
            skills.admit(bad_skill)
        self.assertEqual(str(cm.exception), "missing ['intent', 'evidence']")

    def test_reproduced_integer_one_strict_boolean_identity_precedes_provenance(self):
        # Reproduced integer 1 (not boolean True) combined with absent provenance.source.
        # Proves strict boolean identity (reproduced is True) check precedes provenance rejection.
        bad_skill = {
            "id": "skill-01",
            "intent": "test skill admission",
            "params": {"key": "val"},
            "steps": [{"path": "ax"}],
            "checkpoints": [{"expect": "ok"}],
            "provenance": {},
            "evidence": {"reproduced": 1, "verified_by": "verifier-01"},
        }
        with self.assertRaises(skills.SkillRejected) as cm:
            skills.admit(bad_skill)
        self.assertEqual(
            str(cm.exception),
            "reproduction not verified (evidence.reproduced/verified_by)",
        )

    def test_extra_fields_survive_canonical_serialization(self):
        skill_with_extra = dict(
            VALID_SKILL,
            extra_int=42,
            extra_str="extra_value",
        )
        json_output = skills.admit(skill_with_extra)
        decoded = json.loads(json_output)
        self.assertEqual(decoded["extra_int"], 42)
        self.assertEqual(decoded["extra_str"], "extra_value")

    # Expected-failure methods for malformed inputs/containers

    def test_admit_none_input_raises_skill_rejected(self):
        # DEFECT: admit(None) raises TypeError instead of SkillRejected
        with self.assertRaises(skills.SkillRejected):
            skills.admit(None)

    def test_admit_string_provenance_raises_skill_rejected(self):
        # DEFECT: admit with string provenance raises AttributeError instead of SkillRejected
        bad_skill = dict(VALID_SKILL, provenance="demo:01")
        with self.assertRaises(skills.SkillRejected):
            skills.admit(bad_skill)

    def test_admit_integer_steps_raises_skill_rejected(self):
        # DEFECT: admit with integer steps raises TypeError instead of SkillRejected
        bad_skill = dict(VALID_SKILL, steps=42)
        with self.assertRaises(skills.SkillRejected):
            skills.admit(bad_skill)

    def test_admit_string_step_entries_raises_skill_rejected(self):
        # DEFECT: admit with string step entries raises AttributeError instead of SkillRejected
        bad_skill = dict(VALID_SKILL, steps=["invalid_step_entry"])
        with self.assertRaises(skills.SkillRejected):
            skills.admit(bad_skill)


if __name__ == "__main__":
    unittest.main()
