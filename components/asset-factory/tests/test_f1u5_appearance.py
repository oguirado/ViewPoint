"""Synthetic evidence contract tests, not live appearance discrimination."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gates.appearance_gate import evaluate as gate
from gates.fixtures_negative import registry

core = gate.core


def digest(data):
    return hashlib.sha256(data).hexdigest()


class AppearanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.h = digest(b'synthetic test identity')
        self.policy = {'floor': 4, 'target': 7, 'likeness_floor': 4,
                       'likeness_target': 7, 'timeout_ms': 1000}
        self.profile = {'id': 'synthetic-intake', 'sha256': self.h}
        self.licence = {'schema': 'af.licence-decision/v1', 'rc': 0, 'verdict': 'PASS',
                        'allowed': True, 'provider_id': 'trellis2', 'operation': 'generate',
                        'jurisdiction': 'ES', 'commercial_use': False, 'monetisable': True,
                        'reason_codes': [], 'evaluated_dependencies': [],
                        'configuration_sha256': self.h, 'provider_sha256': self.h,
                        'policy_sha256': self.h}
        asset = self.file('synthetic.asset', b'synthetic asset bytes; not a GLB')
        reference = self.file('synthetic.reference', b'synthetic encoded reference')
        self.frames = {
            'reference': {**self.file('ref.rgb', bytes([255, 0, 0, 0, 255, 0])), 'width': 1, 'height': 2},
            'render': {**self.file('render.rgb', bytes([0, 0, 255, 255, 255, 0])), 'width': 1, 'height': 2},
            'comparison': {**self.file('sheet.rgb', bytes([255, 0, 0, 0, 0, 255, 0, 255, 0, 255, 255, 0])),
                           'width': 2, 'height': 2}}
        self.manifest = {'schema': 'af.appearance-render/v1', 'asset_sha256': asset['sha256'],
                         'reference_sha256': reference['sha256'], 'profile': self.profile.copy(),
                         'render_scope': 'visual_only', 'helpers_excluded': ['_col', '-occ'],
                         'frames': self.frames}
        manifest = self.file('render.json', core.canonical_json(self.manifest).encode())
        self.upstream = {'schema': 'af.bridge-verdict/v1', 'rc': 0, 'verdict': 'PASS',
                         'reason_codes': [], 'source_sha256': asset['sha256'],
                         'profile': self.profile.copy(), 'receipt_file_sha256': self.h,
                         'trace_sha256': self.h, 'failed_checks': []}
        self.request = {'schema': 'af.appearance-request/v1', 'case_id': 'synthetic_good',
                        'asset_type': 'prop', 'evidence_root': str(self.root), 'asset': asset,
                        'reference': reference, 'render_manifest': manifest, 'policy': self.policy,
                        'policy_sha256': core.sha256_json(self.policy), 'profile': self.profile.copy(),
                        'upstream_sha256': core.sha256_json(self.upstream),
                        'builder': {'id': 'synthetic_builder', 'family': 'builder_family',
                                    'revision': 'synthetic-fixed-builder'},
                        'provider': {'id': 'trellis2', 'revision': 'synthetic-fixed-provider'},
                        'licence_decision': self.licence,
                        'licence_decision_sha256': core.sha256_json(self.licence)}
        # Current licence inputs are carried as data and hashed canonically.
        self.licence_context = {
            'provider_manifest': {'schema': 'af.provider/v1', 'id': 'trellis2',
                                  'revision': 'synthetic-fixed-provider', 'slot': 'generate'},
            'configuration': {'jurisdiction': 'ES', 'commercial_use': False,
                              'slots': {'generate': {'provider': 'trellis2'}}},
            'territory_policy': {'schema': 'af.territory-groups/v1', 'countries': ['ES'], 'groups': {}},
            'operation': 'generate'}
        self.request['licence_context'] = self.licence_context
        for field, pin in (('provider_manifest', 'provider_sha256'),
                           ('configuration', 'configuration_sha256'),
                           ('territory_policy', 'policy_sha256')):
            self.licence[pin] = core.sha256_json(self.licence_context[field])
        self.request['licence_decision_sha256'] = core.sha256_json(self.licence)
        self.review = self.make_review()

    def file(self, name, data):
        (self.root / name).write_bytes(data)
        return {'path': name, 'sha256': digest(data)}

    def make_review(self):
        bindings = {'asset_sha256': self.request['asset']['sha256'],
                    'reference_sha256': self.request['reference']['sha256'],
                    'render_manifest_sha256': self.request['render_manifest']['sha256']}
        bindings.update({name + '_rgb_sha256': frame['sha256'] for name, frame in self.frames.items()})
        return {'schema': 'af.appearance-review/v1', 'case_id': self.request['case_id'],
                'asset_type': self.request['asset_type'], 'request_sha256': core.sha256_json(self.request),
                'upstream_sha256': self.request['upstream_sha256'],
                'policy_sha256': self.request['policy_sha256'], 'profile': self.request['profile'].copy(),
                'bindings': bindings,
                'reviewer': {'id': 'synthetic_reviewer', 'family': 'reviewer_family',
                             'revision': 'synthetic-fixed-reviewer'},
                'scores': {name: None if name == 'face_appeal' else 8 for name in gate.SCORES},
                'likeness_to_reference': 8, 'worst_three_problems': [], 'fix_advice': '',
                'verdict': 'PASS', 'min_score': 99}

    def rebind(self):
        self.request['policy_sha256'] = core.sha256_json(self.policy)
        self.request['upstream_sha256'] = core.sha256_json(self.upstream)
        self.request['licence_decision_sha256'] = core.sha256_json(self.licence)
        self.request['render_manifest'] = self.file('render.json', core.canonical_json(self.manifest).encode())
        self.review = self.make_review()

    def check(self, rc, reason=None):
        result = gate.evaluate(self.request, self.upstream, self.review)
        self.assertEqual(result['rc'], rc, result)
        self.assertEqual(result['verdict'], core.VERDICT[rc], result)
        if reason:
            self.assertIn(reason, result['reason_codes'])
        return result

    def test_bound_good_reference_match_passes(self):
        result = self.check(0)
        self.assertEqual(result['min_score'], 8)
        self.assertEqual(result['quality_band'], 'GOOD')
        self.assertEqual(result['bindings']['comparison_rgb_sha256'], self.frames['comparison']['sha256'])

    def test_character_complete_review_passes(self):
        self.request['asset_type'] = 'character'
        self.review = self.make_review()
        self.review['scores']['face_appeal'] = 8
        self.check(0)

    def test_character_face_cannot_be_waived(self):
        self.request['asset_type'] = 'character'
        self.review = self.make_review()
        self.check(3, 'applicable_score_missing')

    def test_missing_criterion_cannot_see(self):
        del self.review['scores']['publishable']
        self.check(3, 'missing_evidence')

    def test_all_null_scores_refused(self):
        self.review['scores'] = dict.fromkeys(gate.SCORES)
        self.check(3, 'applicable_score_missing')

    def test_boolean_float_string_scores_refused(self):
        for value in (True, False, 7.0, '7'):
            with self.subTest(value=repr(value)):
                self.review['scores']['publishable'] = value
                self.check(2, 'score_integer_required')

    def test_out_of_range_score_refused(self):
        for value in (-1, 11):
            with self.subTest(value=value):
                self.review['scores']['publishable'] = value
                self.check(2, 'score_out_of_range')

    def test_wrong_object_high_craft_fails(self):
        self.review['likeness_to_reference'] = 1
        self.check(5, 'likeness_below_floor')

    def test_likeness_target_is_blocking(self):
        self.review['likeness_to_reference'] = 6
        self.check(5, 'likeness_below_target')

    def test_high_average_low_criterion_fails(self):
        self.review['scores']['detail_and_craft'] = 3
        self.check(5, 'craft_below_floor')

    def test_floor_only_is_not_publication_pass(self):
        self.review['scores']['publishable'] = 5
        result = self.check(5, 'publication_target_not_met')
        self.assertEqual(result['quality_band'], 'ACCEPTABLE')

    def test_exact_target_passes(self):
        self.review['scores'] = {key: 7 for key in gate.SCORES}
        self.review['likeness_to_reference'] = 7
        self.check(0)

    def test_supplied_min_score_does_not_override_scores(self):
        self.review['min_score'] = 10
        self.review['scores']['publishable'] = 3
        self.check(5)

    def test_reviewer_veto(self):
        self.review['verdict'] = 'FAIL'
        self.check(5, 'reviewer_veto')

    def test_builder_as_reviewer_refused(self):
        for key in ('id', 'family'):
            with self.subTest(key=key):
                original = self.review['reviewer'][key]
                self.review['reviewer'][key] = self.request['builder'][key]
                self.check(2, 'reviewer_not_independent')
                self.review['reviewer'][key] = original

    def test_reviewer_pass_never_clears_fail(self):
        self.upstream.update(rc=5, verdict='FAIL')
        self.rebind()
        self.check(5, 'upstream_fail')

    def test_all_upstream_refusals_are_preserved(self):
        for rc in (2, 3, 4, 5, 6):
            with self.subTest(rc=rc):
                self.upstream.update(rc=rc, verdict=core.VERDICT[rc])
                self.rebind()
                self.check(rc)

    def test_missing_review_cannot_see(self):
        self.review = None
        self.check(3, 'review_missing')

    def test_missing_receipt_cannot_see(self):
        self.upstream['receipt_file_sha256'] = None
        self.rebind()
        self.check(3, 'upstream_receipt_missing')

    def test_missing_upstream_cannot_see(self):
        self.upstream = None
        self.check(3, 'upstream_missing')

    def test_skip_unclear_cannot_pass(self):
        for value in ('SKIP', 'UNCLEAR', 'CANNOT_SEE', 'ADVISORY'):
            with self.subTest(value=value):
                self.review['verdict'] = value
                self.check(3, 'review_incomplete')

    def test_unknown_review_verdict_errors(self):
        self.review['verdict'] = 'MAYBE'
        self.check(2)

    def test_stage_rc_one_forbidden(self):
        for value in (1, True):
            with self.subTest(value=value):
                self.upstream.update(rc=value, verdict='PASS')
                self.rebind()
                self.check(2, 'upstream_rc_invalid')

    def test_upstream_verdict_must_agree(self):
        self.upstream['verdict'] = 'FAIL'
        self.rebind()
        self.check(2, 'upstream_verdict_invalid')

    def test_raw_bundle_rejected(self):
        self.upstream['schema'] = 'vqa.report/v1'
        self.rebind()
        self.check(2, 'unsupported_upstream_family')

    def test_tampered_reference_or_render_stale(self):
        for path in ('synthetic.reference', 'render.rgb', 'sheet.rgb', 'render.json', 'synthetic.asset'):
            with self.subTest(path=path):
                target = self.root / path
                before = target.read_bytes()
                target.write_bytes(before + b'x')
                self.check(4, 'evidence_stale')
                target.write_bytes(before)

    def test_missing_evidence_cannot_see(self):
        (self.root / 'render.rgb').unlink()
        self.check(3, 'evidence_missing')

    def test_profile_receipt_mismatch_refused(self):
        self.upstream['profile'] = {'id': 'different', 'sha256': self.h}
        self.rebind()
        self.check(4, 'profile_or_source_stale')

    def test_source_receipt_mismatch_refused(self):
        self.upstream['source_sha256'] = digest(b'different asset')
        self.rebind()
        self.check(4, 'profile_or_source_stale')

    def test_upstream_pin_stale(self):
        self.request['upstream_sha256'] = digest(b'other upstream')
        self.review = self.make_review()
        self.check(4, 'upstream_stale')

    def test_policy_changed_without_repin_refused(self):
        self.policy['target'] = 6
        self.check(4, 'policy_stale')

    def test_policy_has_no_defaults(self):
        del self.policy['likeness_target']
        self.rebind()
        self.check(3, 'missing_evidence')

    def test_invalid_policy_refused(self):
        for key, value in (('floor', True), ('target', 11), ('timeout_ms', 0), ('likeness_floor', 10)):
            with self.subTest(key=key):
                before = self.policy[key]
                self.policy[key] = value
                self.rebind()
                self.check(2)
                self.policy[key] = before

    def test_review_binding_each_field_stale(self):
        changes = {'case_id': 'other', 'asset_type': 'character', 'request_sha256': self.h,
                   'upstream_sha256': self.h, 'policy_sha256': self.h,
                   'profile': {'id': 'other', 'sha256': self.h}, 'bindings': {}}
        for key, value in changes.items():
            with self.subTest(key=key):
                before = self.review[key]
                self.review[key] = value
                self.check(4, 'review_binding_stale')
                self.review[key] = before

    def test_visual_only_capture_bound(self):
        self.check(0)

    def test_opaque_helper_hiding_visual_unproven(self):
        self.manifest['render_scope'] = 'all_nodes'
        self.rebind()
        self.check(3, 'visual_only_unproven')

    def test_helpers_exclusion_required(self):
        self.manifest['helpers_excluded'] = []
        self.rebind()
        self.check(3, 'visual_only_unproven')

    def test_render_manifest_binding_stale(self):
        self.manifest['asset_sha256'] = self.h
        self.rebind()
        self.check(4, 'render_binding_stale')

    def test_comparison_reference_first_is_verified(self):
        self.frames['comparison'].update(self.file('sheet.rgb', bytes([0, 0, 255, 255, 0, 0, 255, 255, 0, 0, 255, 0])))
        self.rebind()
        self.check(4, 'reference_first_comparison_stale')

    def test_rgb_dimensions_and_size(self):
        self.frames['render']['width'] = True
        self.rebind()
        self.check(2, 'rgb_dimensions_invalid')
        self.frames['render']['width'] = 2
        self.rebind()
        self.check(2, 'rgb_size_invalid')

    def test_escaping_path_refused(self):
        self.request['asset']['path'] = '../outside.asset'
        self.review = self.make_review()
        self.check(2, 'path_escape')

    def test_symlink_refused(self):
        (self.root / 'linked').symlink_to(self.root / 'synthetic.asset')
        self.request['asset']['path'] = 'linked'
        self.review = self.make_review()
        self.check(2)

    def test_symlink_parent_refused(self):
        (self.root / 'linked').symlink_to(self.root, target_is_directory=True)
        self.request['asset']['path'] = 'linked/synthetic.asset'
        self.review = self.make_review()
        self.check(2)

    def test_timeout_budget_exceeded(self):
        with mock.patch.object(gate.time, 'monotonic_ns', side_effect=[0, 1000000001]):
            self.check(6, 'deadline_exceeded')

    def test_evidence_budget_exceeded(self):
        with mock.patch.object(gate, 'MAX_TOTAL_BYTES', 1):
            self.check(6, 'evidence_budget_exceeded')

    def test_rgb_budget_exceeded(self):
        self.frames['reference']['width'] = gate.MAX_EVIDENCE_BYTES
        self.rebind()
        self.check(6, 'rgb_budget_exceeded')

    def test_unknown_properties_rejected(self):
        self.review['invented'] = 'not allowed'
        self.check(2, 'unknown_property')

    def test_unpinned_reviewer_revision(self):
        self.review['reviewer']['revision'] = 'main'
        self.check(2, 'unpinned_revision')

    def test_unpinned_provider_revision(self):
        self.request['provider']['revision'] = 'HEAD'
        self.review = self.make_review()
        self.check(2, 'unpinned_revision')

    def test_licence_refusal_is_informational(self):
        self.licence.update(rc=5, verdict='FAIL', allowed=False)
        self.rebind()
        self.check(0)

    def test_omitted_appearance_declarations_pass(self):
        config = self.licence_context["configuration"]
        for field in ("jurisdiction", "commercial_use"):
            del config[field]
            self.licence[field] = None
        self.licence["configuration_sha256"] = core.sha256_json(config)
        self.rebind()
        self.check(0)
        for field in ("jurisdiction", "commercial_use"):
            del self.licence[field]
        self.rebind()
        self.check(0)
        self.assertEqual(self.review["request_sha256"], core.sha256_json(self.request))

    def test_appearance_declaration_types_are_checked_on_resealed_inputs(self):
        config = self.licence_context["configuration"]
        for field, bad in (("jurisdiction", 3), ("commercial_use", 1)):
            with self.subTest(location="configuration", field=field):
                old = config[field]
                config[field] = bad
                self.licence["configuration_sha256"] = core.sha256_json(config)
                self.rebind()
                self.check(2, "schema_mismatch")
                config[field] = old
                self.licence["configuration_sha256"] = core.sha256_json(config)
            with self.subTest(location="decision", field=field):
                old = self.licence[field]
                self.licence[field] = bad
                self.rebind()
                self.check(2, "schema_mismatch")
                self.licence[field] = old

    def test_present_appearance_declaration_must_match_current_configuration(self):
        for field, changed in (("jurisdiction", "other"), ("commercial_use", True)):
            with self.subTest(field=field):
                old = self.licence[field]
                self.licence[field] = changed
                self.rebind()
                self.check(4, "licence_context_stale")
                self.licence[field] = old

    def test_provider_name_is_not_a_licence_gate(self):
        self.request['provider']['id'] = 'hunyuan3d21'
        self.licence['provider_id'] = 'hunyuan3d21'
        self.request['licence_context']['provider_manifest']['id'] = 'hunyuan3d21'
        assignment = self.request['licence_context']['configuration']['slots']['generate']
        assignment['provider'] = 'hunyuan3d21'
        self.licence['provider_sha256'] = core.sha256_json(self.request['licence_context']['provider_manifest'])
        self.licence['configuration_sha256'] = core.sha256_json(self.request['licence_context']['configuration'])
        self.review = self.make_review()
        self.rebind()
        self.check(0)

    def test_licence_decision_pin_stale(self):
        self.licence['jurisdiction'] = 'US'
        self.check(4, 'licence_decision_stale')

    def test_invalid_licence_decision_refused(self):
        self.licence['rc'] = True
        self.rebind()
        self.check(2, 'licence_decision_invalid')

    def test_malformed_policy_pin_still_emits_json(self):
        args, output = self.cli_files()
        self.request['policy_sha256'] = {'float': 1.5}
        # Disk parser rejects floats; direct library refusal must also be serializable.
        result = gate.evaluate(self.request, self.upstream, self.review)
        self.assertEqual(result['rc'], 4)
        self.assertIsNone(result['policy_sha256'])
        core.canonical_json(result)

    def test_invalid_inputs_return_stage_error(self):
        for item in (None, [], True, 'request'):
            with self.subTest(item=item):
                self.assertEqual(gate.evaluate(item, None, None)['rc'], 2)

    def test_asset_type_invalid(self):
        self.request['asset_type'] = 'unknown'
        self.review = self.make_review()
        self.check(2, 'asset_type_invalid')

    def test_request_family_invalid(self):
        self.request['schema'] = 'af.unknown/v1'
        self.review = self.make_review()
        self.check(2, 'unsupported_request_family')

    def test_review_family_invalid(self):
        self.review['schema'] = 'af.unknown/v1'
        self.check(2, 'unsupported_review_family')

    def test_render_family_invalid(self):
        self.manifest['schema'] = 'af.unknown/v1'
        self.rebind()
        self.check(2, 'unsupported_render_family')

    def test_upstream_lists_invalid(self):
        self.upstream['reason_codes'] = None
        self.rebind()
        self.check(2, 'upstream_invalid')

    def test_upstream_pass_with_failed_checks_invalid(self):
        self.upstream['failed_checks'] = ['failed']
        self.rebind()
        self.check(2, 'upstream_pass_with_failures')

    def test_identity_empty_text_refused(self):
        self.request['builder']['id'] = ''
        self.review = self.make_review()
        self.check(2, 'text_required')

    def test_digest_invalid(self):
        self.upstream['trace_sha256'] = 'invalid'
        self.rebind()
        self.check(2, 'digest_invalid')

    def test_relative_root_invalid(self):
        self.request['evidence_root'] = '.'
        self.review = self.make_review()
        self.check(2, 'root_not_absolute')

    def test_nonregular_fifo_rejected_without_blocking(self):
        os.mkfifo(self.root / 'fifo')
        self.request['asset']['path'] = 'fifo'
        self.review = self.make_review()
        self.check(2, 'not_regular_file')

    def test_licence_provider_binding_stale(self):
        self.licence['provider_id'] = 'other'
        self.rebind()
        self.check(4, 'licence_provider_stale')

    def test_review_advice_invalid(self):
        self.review['worst_three_problems'] = [False]
        self.check(2, 'review_advice_invalid')

    def test_comparison_dimensions_invalid(self):
        self.frames['comparison'].update(width=1, height=4)
        self.rebind()
        self.check(2, 'comparison_dimensions_invalid')

    def test_duplicate_manifest_key_invalid(self):
        data = core.canonical_json(self.manifest).rstrip()[:-1] + ',"schema":"af.appearance-render/v1"}'
        self.request['render_manifest'] = self.file('render.json', data.encode())
        self.review = self.make_review()
        self.check(2, 'duplicate_key')

    def test_schema_ids_and_record_fields_match(self):
        samples = {'appearance-request': self.request, 'appearance-review': self.review,
                   'appearance-render': self.manifest, 'appearance-result': self.check(0)}
        for name, sample in samples.items():
            with self.subTest(schema=name):
                schema = core.load_json(ROOT / ('gates/appearance_gate/' + name + '.schema.json'),
                                        max_bytes=gate.MAX_JSON_BYTES)
                self.assertEqual(schema['$id'], 'urn:af:' + name + ':v1')
                self.assertEqual(schema['properties']['schema']['const'], sample['schema'])
                self.assertLessEqual(set(schema['required']), sample.keys())
                self.assertLessEqual(sample.keys(), schema['properties'].keys())
                self.assertFalse(schema['additionalProperties'])
        self.review = None
        result = self.check(3)
        self.assertEqual(set(result), set(samples['appearance-result']))

    def cli_files(self):
        args = []
        for name, value in (('request', self.request), ('upstream', self.upstream), ('review', self.review)):
            path = self.root / (name + '.json')
            path.write_text(core.canonical_json(value))
            args.extend(['--' + name, str(path)])
        output = self.root / 'result.json'
        return args + ['--out', str(output)], output

    def test_valid_result_published_atomically(self):
        args, output = self.cli_files()
        output.write_bytes(b'previous')
        original_replace = core.os.replace
        with mock.patch.object(core.os, 'replace', wraps=original_replace) as replace:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate.main(args), 0)
            self.assertEqual(replace.call_count, 1)
        data = output.read_bytes()
        self.assertEqual(data, core.canonical_json(json.loads(data)).encode())

    def test_refusal_preserves_existing_bytes(self):
        for rc in (2, 3, 4, 5, 6):
            with self.subTest(rc=rc):
                self.upstream.update(rc=rc, verdict=core.VERDICT[rc])
                self.rebind()
                args, output = self.cli_files()
                output.write_bytes(b'caller bytes\x00')
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate.main(args), rc)
                self.assertEqual(output.read_bytes(), b'caller bytes\x00')

    def test_failed_replace_preserves_existing_bytes(self):
        args, output = self.cli_files()
        output.write_bytes(b'caller bytes')
        with mock.patch.object(core.os, 'replace', side_effect=OSError('planted rename failure')):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate.main(args), 2)
        self.assertEqual(output.read_bytes(), b'caller bytes')
        self.assertEqual(list(self.root.glob('.result.json.*')), [])

    def test_cli_isolated_python(self):
        args, output = self.cli_files()
        proc = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'gates/appearance_gate/evaluate.py'), *args],
                              capture_output=True, timeout=10, start_new_session=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['rc'], 0)
        self.assertTrue(output.exists())

    def test_cli_malformed_usage_stage_two(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main([]), 2)

    def test_duplicate_float_nan_json_refused(self):
        args, output = self.cli_files()
        for data in ('{"a":1,"a":2}', '{"a":7.0}', '{"a":NaN}'):
            with self.subTest(data=data):
                (self.root / 'request.json').write_text(data)
                output.write_bytes(b'caller bytes')
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate.main(args), 2)
                self.assertEqual(output.read_bytes(), b'caller bytes')

    def test_input_json_bounded(self):
        args, output = self.cli_files()
        valid = (self.root / 'request.json').read_bytes()
        (self.root / 'request.json').write_bytes(valid + b' ' * (gate.MAX_JSON_BYTES + 1 - len(valid)))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(args), 2)
        self.assertFalse(output.exists())

    def test_strict_json_loader_rejects_duplicate_and_float(self):
        path = self.root / 'strict.json'
        for text in ('{"a":1,"a":2}', '{"a":7.0}', '{"a":NaN}'):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(core.ContractError):
                    core.load_json(path, max_bytes=gate.MAX_JSON_BYTES)

    def test_missing_cli_receipt_preserves_output(self):
        args, output = self.cli_files()
        (self.root / 'upstream.json').unlink()
        output.write_bytes(b'previous')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(args), 3)
        self.assertEqual(output.read_bytes(), b'previous')

    def test_inputs_not_mutated(self):
        before = copy.deepcopy((self.request, self.upstream, self.review))
        self.check(0)
        self.assertEqual((self.request, self.upstream, self.review), before)


class FixtureTests(AppearanceTests):
    # Reuse data setup only; unittest discovery below removes inherited tests.
    def test_original_registry_obligations_cannot_see(self):
        data = core.load_json(ROOT / 'gates/fixtures_negative/registry.json', max_bytes=gate.MAX_JSON_BYTES)
        self.assertEqual(data['schema'], 'af.fixtures-negative/v1')
        self.assertEqual(tuple(entry['id'] for entry in data['fixtures']), registry.ORIGINAL_CASES)
        observed = []
        with mock.patch.object(registry, 'evaluate', side_effect=AssertionError('blocked original evaluated')):
            for entry in data['fixtures']:
                with self.subTest(case=entry['id']):
                    item = registry.assess_fixture(entry, self.request, self.upstream, self.review)
                    self.assertEqual(item['rc'], 3)
                    self.assertFalse(entry['execution_allowed'])
                    observed.append(item)
        self.assertEqual(registry._reduce_unchecked_observations(observed)['rc'], 3)

    def cleared(self):
        return {'id': self.request['case_id'], 'asset_ref': self.request['asset'],
                'reference_ref': self.request['reference'], 'provider': 'trellis2',
                'revision': 'synthetic-fixed-provider', 'licence': 'MIT',
                'provenance_state': 'VERIFIED', 'execution_allowed': True, 'substitute_for': None}

    def test_cleared_synthetic_negative_observed_fail(self):
        self.review['likeness_to_reference'] = 1
        observed = registry.assess_fixture(self.cleared() | {'licence': 'unproven'},
                                           self.request, self.upstream, self.review)
        self.assertEqual(observed['rc'], 5)
        self.assertTrue(observed['bindings'])

    def test_cleared_fixture_licence_and_former_provider_denylist_are_advisory(self):
        entry = self.cleared() | {"licence": "unknown"}
        result = registry.assess_fixture(entry, self.request, self.upstream, self.review)
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(result["bindings"], self.review["bindings"])

        provider = "hunyuan3d21"
        entry["provider"] = provider
        self.request["provider"]["id"] = provider
        self.licence["provider_id"] = provider
        self.licence_context["provider_manifest"]["id"] = provider
        self.licence_context["configuration"]["slots"]["generate"]["provider"] = provider
        self.licence["provider_sha256"] = core.sha256_json(self.licence_context["provider_manifest"])
        self.licence["configuration_sha256"] = core.sha256_json(self.licence_context["configuration"])
        self.rebind()
        result = registry.assess_fixture(entry, self.request, self.upstream, self.review)
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(result["bindings"], self.review["bindings"])

    def test_blocked_and_unknown_provenance_refused(self):
        for key, value in (('execution_allowed', False),
                           ('provenance_state', 'CONFLICTED'), ('revision', 'latest')):
            with self.subTest(key=key):
                entry = self.cleared()
                entry[key] = value
                with mock.patch.object(registry, 'evaluate', side_effect=AssertionError('must not evaluate')):
                    self.assertEqual(registry.assess_fixture(entry, self.request)['rc'], 3)

    def test_fixture_binding_stale(self):
        entry = self.cleared()
        entry['id'] = 'another case'
        self.assertEqual(registry.assess_fixture(entry, self.request)['rc'], 4)

    def test_substitute_does_not_satisfy_original(self):
        bindings = self.check(0)['bindings']
        observations = [{'case_id': case, 'rc': 5, 'verdict': 'FAIL', 'bindings': bindings,
                         'substitute_for': 'other case'} for case in registry.ORIGINAL_CASES]
        self.assertEqual(registry._reduce_unchecked_observations(observations)['rc'], 3)

    def test_bound_observed_originals_close_coverage(self):
        # Ledger aggregation test only: these are invented observations, not live originals.
        bindings = self.check(0)['bindings']
        observations = [{'case_id': case, 'rc': 5, 'verdict': 'FAIL', 'bindings': bindings,
                         'substitute_for': None} for case in registry.ORIGINAL_CASES]
        self.assertEqual(registry._reduce_unchecked_observations(observations)['rc'], 0)
        observations[0].update(rc=0, verdict='PASS')
        self.assertEqual(registry._reduce_unchecked_observations(observations)['rc'], 5)

    def test_empty_binding_cannot_establish_observation(self):
        observations = [{'case_id': case, 'rc': 5, 'verdict': 'FAIL', 'bindings': {'x': self.h},
                         'substitute_for': None} for case in registry.ORIGINAL_CASES]
        self.assertEqual(registry._reduce_unchecked_observations(observations)['rc'], 3)

    def test_coverage_error_precedes_false_pass(self):
        observations = [{'case_id': registry.ORIGINAL_CASES[0], 'rc': 2, 'verdict': 'ERROR'},
                        {'case_id': registry.ORIGINAL_CASES[1], 'rc': 0, 'verdict': 'PASS'}]
        self.assertEqual(registry._reduce_unchecked_observations(observations)['rc'], 2)

    def test_missing_or_duplicate_cases_do_not_pass(self):
        self.assertEqual(registry._reduce_unchecked_observations([])['rc'], 3)
        self.assertEqual(registry._reduce_unchecked_observations([{'case_id': 'x'}, {'case_id': 'x'}])['rc'], 2)


def load_tests(loader, tests, pattern):
    # Keep one execution of each contract test; FixtureTests only adds fixture cases.
    suite = loader.loadTestsFromTestCase(AppearanceTests)
    for name in FixtureTests.__dict__:
        if name.startswith('test_'):
            suite.addTest(FixtureTests(name))
    return suite


if __name__ == '__main__':
    unittest.main()
