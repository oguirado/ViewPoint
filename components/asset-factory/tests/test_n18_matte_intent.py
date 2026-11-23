"""Fine-structure alpha intent at the INPUT matte boundary."""
import unittest

import test_d18_input_contract as fixtures


class FineStructureIntent(unittest.TestCase):
    def setUp(self):
        self.matte = fixtures.Mattes()
        self.matte.setUp()
        self.matte.intent["regions"]["fine_structure"] = [[3, 2, 4, 3]]

    def admit(self):
        return self.matte.admit(self.matte.payload())

    def test_declared_fractional_edge_passes(self):
        self.matte.intent["fine_structure_alpha"] = "fractional"
        result = self.admit()
        self.assertEqual(result["source_rc"], 0, result)

    def test_newly_pinned_binary_master_refused_with_or_without_explicit_field(self):
        self.matte.alpha[2 * 8 + 3] = 255
        for explicit in (False, True):
            if explicit:
                self.matte.intent["fine_structure_alpha"] = "fractional"
            with self.subTest(explicit=explicit):
                result = self.admit()
                self.assertEqual(result["source_rc"], 5, result)
                self.assertEqual(result["reason_codes"], ["protected_fine_structure_alpha_binarized"])

    def test_fractional_alpha_elsewhere_cannot_cover_a_second_region(self):
        self.matte.intent["fine_structure_alpha"] = "fractional"
        self.matte.intent["regions"]["fine_structure"].append([2, 2, 3, 3])
        self.assertEqual(self.admit()["reason_codes"], ["protected_fine_structure_alpha_binarized"])

    def test_explicit_policy_cannot_disable_fractional_alpha(self):
        self.matte.intent["fine_structure_alpha"] = "binary"
        result = self.admit()
        self.assertEqual(result["source_rc"], 2, result)
        self.assertEqual(result["reason_codes"], ["invalid_fine_structure_alpha_intent"])

    def test_no_fine_structure_leaves_binary_matte_eligible(self):
        self.matte.intent["regions"]["fine_structure"] = []
        self.matte.alpha[2 * 8 + 3] = 255
        result = self.admit()
        self.assertEqual(result["source_rc"], 0, result)
