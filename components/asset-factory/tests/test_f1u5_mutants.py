"""Execute planted false-PASS faults against the unchanged negative assertions.

Each targeted refusal is temporarily mapped to rc 0 in an in-memory copy of the
production module. A hit counter proves the intended fault ran. The ordinary
negative test must then fail an assertion; import errors do not kill a mutant.
"""
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('f1u5_mutant_oracles', TEST_DIR / 'test_f1u5_appearance.py')
oracles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracles)

CASES = {
    'applicable_score_missing': 'test_character_face_cannot_be_waived',
    'missing_evidence': 'test_missing_criterion_cannot_see',
    'score_integer_required': 'test_boolean_float_string_scores_refused',
    'score_out_of_range': 'test_out_of_range_score_refused',
    'likeness_below_floor': 'test_wrong_object_high_craft_fails',
    'likeness_below_target': 'test_likeness_target_is_blocking',
    'craft_below_floor': 'test_high_average_low_criterion_fails',
    'publication_target_not_met': 'test_floor_only_is_not_publication_pass',
    'reviewer_veto': 'test_reviewer_veto',
    'reviewer_not_independent': 'test_builder_as_reviewer_refused',
    'upstream_receipt_missing': 'test_missing_receipt_cannot_see',
    'upstream_missing': 'test_missing_upstream_cannot_see',
    'review_missing': 'test_missing_review_cannot_see',
    'review_incomplete': 'test_skip_unclear_cannot_pass',
    'review_verdict_invalid': 'test_unknown_review_verdict_errors',
    'upstream_rc_invalid': 'test_stage_rc_one_forbidden',
    'upstream_verdict_invalid': 'test_upstream_verdict_must_agree',
    'unsupported_upstream_family': 'test_raw_bundle_rejected',
    'evidence_stale': 'test_tampered_reference_or_render_stale',
    'profile_or_source_stale': 'test_profile_receipt_mismatch_refused',
    'upstream_stale': 'test_upstream_pin_stale',
    'policy_stale': 'test_policy_changed_without_repin_refused',
    'policy_integer_required': 'test_invalid_policy_refused',
    'policy_invalid': 'test_invalid_policy_refused',
    'review_binding_stale': 'test_review_binding_each_field_stale',
    'visual_only_unproven': 'test_opaque_helper_hiding_visual_unproven',
    'render_binding_stale': 'test_render_manifest_binding_stale',
    'reference_first_comparison_stale': 'test_comparison_reference_first_is_verified',
    'rgb_dimensions_invalid': 'test_rgb_dimensions_and_size',
    'rgb_size_invalid': 'test_rgb_dimensions_and_size',
    'path_escape': 'test_escaping_path_refused',
    'deadline_exceeded': 'test_timeout_budget_exceeded',
    'evidence_budget_exceeded': 'test_evidence_budget_exceeded',
    'rgb_budget_exceeded': 'test_rgb_budget_exceeded',
    'unknown_property': 'test_unknown_properties_rejected',
    'unpinned_revision': 'test_unpinned_reviewer_revision',
    'licence_refused': 'test_licence_refusal',
    'provider_blocked': 'test_hunyuan_never_executed',
    'licence_decision_stale': 'test_licence_decision_pin_stale',
    'licence_decision_invalid': 'test_invalid_licence_decision_refused',
    'object_required': 'test_invalid_inputs_return_stage_error',
    'asset_type_invalid': 'test_asset_type_invalid',
    'unsupported_request_family': 'test_request_family_invalid',
    'unsupported_review_family': 'test_review_family_invalid',
    'unsupported_render_family': 'test_render_family_invalid',
    'upstream_invalid': 'test_upstream_lists_invalid',
    'upstream_pass_with_failures': 'test_upstream_pass_with_failed_checks_invalid',
    'text_required': 'test_identity_empty_text_refused',
    'digest_invalid': 'test_digest_invalid',
    'root_not_absolute': 'test_relative_root_invalid',
    'not_regular_file': 'test_nonregular_fifo_rejected_without_blocking',
    'licence_provider_stale': 'test_licence_provider_binding_stale',
    'review_advice_invalid': 'test_review_advice_invalid',
    'comparison_dimensions_invalid': 'test_comparison_dimensions_invalid',
    'duplicate_key': 'test_duplicate_manifest_key_invalid',
}


class MutationControls(unittest.TestCase):
    def test_targeted_false_pass_mutants_go_red(self):
        source = Path(oracles.gate.__file__).read_text()
        anchor = '        raise Refusal(rc, reason)'
        self.assertEqual(source.count(anchor), 1)
        for reason, test_name in CASES.items():
            with self.subTest(mutant=reason):
                replacement = (f'        if reason == {reason!r}:\n'
                               '            globals()["_mutant_hits"] += 1\n'
                               '            raise Refusal(0, reason)\n' + anchor)
                mutant = types.ModuleType('gates.appearance_gate._mutant')
                mutant.__file__ = oracles.gate.__file__
                mutant.__package__ = 'gates.appearance_gate'
                mutant._mutant_hits = 0
                exec(compile(source.replace(anchor, replacement), mutant.__file__, 'exec'), mutant.__dict__)
                stream = io.StringIO()
                with mock.patch.object(oracles, 'gate', mutant):
                    result = unittest.TextTestRunner(stream=stream).run(oracles.AppearanceTests(test_name))
                self.assertGreater(mutant._mutant_hits, 0, 'intended mutation never executed')
                self.assertFalse(result.wasSuccessful(), 'surviving mutant: ' + reason)
                self.assertTrue(result.failures, stream.getvalue())
                self.assertFalse(result.errors, 'runtime errors are not a killed mutant: ' + stream.getvalue())
                print('MUTANT_KILLED ' + reason + ' oracle=' + test_name)

    def test_upstream_absorbing_and_publication_mutants_go_red(self):
        source = Path(oracles.gate.__file__).read_text()
        for name, old, new, test_name in (
            ('clear_upstream_fail', "    if upstream['rc'] != 0:", '    if False:',
             'test_reviewer_pass_never_clears_fail'),
            ('publish_refusal', "        if result['rc'] == 0:", '        if True:',
             'test_refusal_preserves_existing_bytes'),
            ('missing_bytes_pass', "        rc, reasons = 3, ['evidence_missing']",
             "        rc, reasons = 0, ['evidence_missing']", 'test_missing_evidence_cannot_see'),
        ):
            with self.subTest(mutant=name):
                self.assertEqual(source.count(old), 1)
                mutant = types.ModuleType('gates.appearance_gate._mutant')
                mutant.__file__, mutant.__package__ = oracles.gate.__file__, 'gates.appearance_gate'
                exec(compile(source.replace(old, new), mutant.__file__, 'exec'), mutant.__dict__)
                stream = io.StringIO()
                with mock.patch.object(oracles, 'gate', mutant):
                    result = unittest.TextTestRunner(stream=stream).run(oracles.AppearanceTests(test_name))
                self.assertTrue(result.failures, stream.getvalue())
                self.assertFalse(result.errors, stream.getvalue())
                print('MUTANT_KILLED ' + name + ' oracle=' + test_name)

    def test_kernel_read_bounds_and_grammar_mutants_go_red(self):
        # Change the called kernel only for a bounded ordinary negative test.
        original = oracles.gate.core.load_json
        for name, test_name, replacement in (
            ('unbounded_json', 'test_input_json_bounded',
             lambda path, *, max_bytes: original(path, max_bytes=max_bytes * 2)),
            ('loose_json_grammar', 'test_strict_json_loader_rejects_duplicate_and_float',
             lambda path, *, max_bytes: __import__('json').loads(Path(path).read_bytes())),
        ):
            with self.subTest(mutant=name):
                stream = io.StringIO()
                with mock.patch.object(oracles.gate.core, 'load_json', side_effect=replacement):
                    result = unittest.TextTestRunner(stream=stream).run(oracles.AppearanceTests(test_name))
                self.assertTrue(result.failures, stream.getvalue())
                self.assertFalse(result.errors, stream.getvalue())
                print('MUTANT_KILLED ' + name + ' oracle=' + test_name)

    def test_symlink_protection_mutants_go_red(self):
        source = Path(oracles.gate.__file__).read_text()
        self.assertIn(' | os.O_NOFOLLOW', source)
        for test_name in ('test_symlink_refused', 'test_symlink_parent_refused'):
            with self.subTest(mutant=test_name):
                mutant = types.ModuleType('gates.appearance_gate._mutant')
                mutant.__file__, mutant.__package__ = oracles.gate.__file__, 'gates.appearance_gate'
                exec(compile(source.replace(' | os.O_NOFOLLOW', ''), mutant.__file__, 'exec'), mutant.__dict__)
                stream = io.StringIO()
                with mock.patch.object(oracles, 'gate', mutant):
                    result = unittest.TextTestRunner(stream=stream).run(oracles.AppearanceTests(test_name))
                self.assertTrue(result.failures, stream.getvalue())
                self.assertFalse(result.errors, stream.getvalue())
                print('MUTANT_KILLED nofollow_' + test_name + ' oracle=' + test_name)

    def test_fixture_licence_and_substitute_mutants_go_red(self):
        source = Path(oracles.registry.__file__).read_text()
        for name, old, new, test_name in (
            ('admit_blocked_original', "    if not (entry.get('execution_allowed') is True and", '    if not (True or',
             'test_original_registry_obligations_cannot_see'),
            ('substitute_closes_original', "        if item is None or item.get('substitute_for') is not None:",
             '        if item is None:', 'test_substitute_does_not_satisfy_original'),
            ('unbound_closes_original', "and _bound(item.get('bindings')):",
             'and True:', 'test_empty_binding_cannot_establish_observation'),
        ):
            with self.subTest(mutant=name):
                self.assertEqual(source.count(old), 1)
                mutant = types.ModuleType('gates.fixtures_negative._mutant')
                exec(compile(source.replace(old, new), str(oracles.registry.__file__), 'exec'), mutant.__dict__)
                stream = io.StringIO()
                with mock.patch.object(oracles, 'registry', mutant):
                    result = unittest.TextTestRunner(stream=stream).run(oracles.FixtureTests(test_name))
                self.assertTrue(result.failures, stream.getvalue())
                self.assertFalse(result.errors, stream.getvalue())
                print('MUTANT_KILLED ' + name + ' oracle=' + test_name)


if __name__ == '__main__':
    unittest.main()
