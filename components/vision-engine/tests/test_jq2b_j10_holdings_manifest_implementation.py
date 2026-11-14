"""J10 manifest owner gate: real API, independent intent and negative contrasts."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import numpy as np

from eval.v2eval.holdings_manifest import (
    ManifestError, build_manifest, derive_case_seed, parse_metadata,
    pixel_declaration_sha256, verify_manifest,
)

_SOURCE = b'generator:isolated-contract-v1\n'
_SOURCE_HASH = hashlib.sha256(_SOURCE).hexdigest()
_ENV = {'python': '3.12.1', 'numpy': '2.0.0', 'platform': 'linux-x86_64',
        'dependency_digest': hashlib.sha256(b'visioncore-pinned').hexdigest()}
_A = {'kind': 'godot_window', 'canonical_id': 'a', 'generation': 1}
_B = {'kind': 'cdp_tab', 'canonical_id': 'b', 'generation': 1}


def _frame(seq, pixel, width=4, height=4):
    # Independent hardcoded generation intent: no GM/FL classifier is called.
    img = np.full((height, width, 3), pixel, dtype=np.uint8)
    return {'frame_seq': seq, 'rgb': img, 'raw_sha256': hashlib.sha256(img.tobytes()).hexdigest(),
            'pixel_decl_sha256': pixel_declaration_sha256(width, height)}


def _case(cid, label, seqs=(0, 1), width=4, height=4):
    frames = [_frame(seq, (seq + 1) * 37, width, height) for seq in seqs]
    return {'case_id': cid, 'seed_tag': 7, 'geometry': {'width': width, 'height': height},
            'faults': [label], 'expected': {'label': label, 'dx': None, 'dy': None,
                                            'gm_ops': None, 'fl_ops': None},
            'expected_frame_count': len(seqs), 'expected_frame_seqs': list(seqs),
            'frames': frames}


def _corpus():
    cases = [_case('static', 'static'),
             _case('motion', 'integer_translation', (0, 2, 4)),
             _case('flicker', 'real_motion_veto', (0, 1, 2)),
             _case('resize', 'resize_epoch', (0,), width=6, height=5)]
    lanes = [
        {'handle': _A, 'case_ids': ['static', 'motion', 'resize'], 'epoch_plan': [
            {'case_id': 'static', 'epoch': 0, 'frame_seqs': [0, 1]},
            {'case_id': 'motion', 'epoch': 0, 'frame_seqs': [0, 2, 4]},
            {'case_id': 'resize', 'epoch': 1, 'frame_seqs': [0]}]},
        {'handle': _B, 'case_ids': ['flicker'], 'epoch_plan': [
            {'case_id': 'flicker', 'epoch': 0, 'frame_seqs': [0, 1, 2]}]},
    ]
    return cases, lanes


def _build(cases=None, lanes=None, **kw):
    default_cases, default_lanes = _corpus()
    args = {'environment': _ENV, 'lanes': default_lanes if lanes is None else lanes,
            'generator_source': _SOURCE, 'generator_sha256': _SOURCE_HASH,
            'algorithm_id': 's2-gmfl-pinned', 'master_seed': 42}
    args.update(kw)
    return build_manifest(default_cases if cases is None else cases, **args)


class HoldingsManifestGate(unittest.TestCase):
    def test_canonical_manifest_approved_fields_order_and_known_frame_digest(self):
        m = _build()
        doc = m.metadata()
        self.assertTrue(verify_manifest(m.canonical_json, m.sha256))
        self.assertEqual(set(doc), {'schema', 'generator_sha256', 'algorithm_id',
                                    'master_seed', 'environment', 'lanes', 'cases', 'frames'})
        self.assertEqual(len(doc['frames']), 9)
        self.assertEqual([c['case_id'] for c in doc['cases']], ['flicker', 'motion', 'resize', 'static'])
        self.assertEqual(doc['frames'][0]['raw_sha256'], hashlib.sha256(bytes([37])*48).hexdigest())
        self.assertEqual(doc['cases'][0]['expected']['gm_ops'], None)
        self.assertEqual(doc['cases'][0]['expected']['fl_ops'], None)
        self.assertNotIn('rgb', m.canonical_json)
        self.assertNotIn('normalized_rgb', m.canonical_json)
        self.assertEqual(m.sha256, hashlib.sha256(m.canonical_json.encode('ascii')).hexdigest())

    def test_shuffled_equivalent_corpus_and_lane_isolation(self):
        cases, lanes = _corpus()
        expected = _build(cases, lanes).sha256
        cases.reverse()
        for c in cases:
            c['frames'].reverse()
            c['faults'].reverse()
            c['expected_frame_seqs'].reverse()
        lanes.reverse()
        for l in lanes:
            l['epoch_plan'].reverse()
            l['case_ids'].reverse()
            for plan in l['epoch_plan']:
                plan['frame_seqs'].reverse()
        self.assertEqual(_build(cases, lanes).sha256, expected)
        seed_a = derive_case_seed(42, handle=_A, case_id='static', seed_tag=7,
                                  generator_sha256=_SOURCE_HASH, environment=_ENV)
        seed_b = derive_case_seed(42, handle=_B, case_id='static', seed_tag=7,
                                  generator_sha256=_SOURCE_HASH, environment=_ENV)
        self.assertEqual(seed_a, derive_case_seed(42, handle=_A, case_id='static', seed_tag=7,
                                                  generator_sha256=_SOURCE_HASH, environment=_ENV))
        self.assertNotEqual(seed_a, seed_b)

    def test_generator_and_environment_identity_negative_contrast(self):
        baseline = _build().sha256
        with self.assertRaisesRegex(ManifestError, 'generator source digest'):
            _build(generator_source=b'changed generator')
        alt_source = b'changed generator'
        self.assertNotEqual(_build(generator_source=alt_source,
                                   generator_sha256=hashlib.sha256(alt_source).hexdigest()).sha256,
                            baseline)
        alt_env = dict(_ENV, dependency_digest=hashlib.sha256(b'changed dependency').hexdigest())
        self.assertNotEqual(_build(environment=alt_env).sha256, baseline)
        self.assertNotEqual(_build(master_seed=43).sha256, baseline)
        self.assertNotEqual(_build(algorithm_id='s2-gmfl-new').sha256, baseline)
        with self.assertRaises(ManifestError):
            _build(environment={k: v for k, v in _ENV.items() if k != 'dependency_digest'})

    def test_frame_completeness_and_duplicates_positive_negative(self):
        cases, lanes = _corpus()
        self.assertTrue(_build(cases, lanes).sha256)
        cases[0]['frames'].pop()
        with self.assertRaisesRegex(ManifestError, 'observed frames'):
            _build(cases, lanes)
        cases, lanes = _corpus()
        cases.append(copy.deepcopy(cases[0]))
        with self.assertRaisesRegex(ManifestError, 'duplicate case'):
            _build(cases, lanes)
        cases, lanes = _corpus()
        cases[1]['frames'][1]['frame_seq'] = cases[1]['frames'][0]['frame_seq']
        with self.assertRaisesRegex(ManifestError, 'observed frames'):
            _build(cases, lanes)
        cases, lanes = _corpus()
        lanes[0]['epoch_plan'][1]['frame_seqs'] = [0, 1, 4]
        with self.assertRaisesRegex(ManifestError, 'epoch plan sequence'):
            _build(cases, lanes)

    def test_layout_hash_content_and_normalized_optional(self):
        cases, lanes = _corpus()
        target = cases[0]['frames'][0]
        target['normalized_rgb'] = target['rgb'].copy()
        target['normalized_sha256'] = target['raw_sha256']
        self.assertEqual(_build(cases, lanes).metadata()['frames'][-2]['normalized_sha256'],
                         target['raw_sha256'])
        target['pixel_decl_sha256'] = '0' * 64
        with self.assertRaisesRegex(ManifestError, 'pixel declaration'):
            _build(cases, lanes)
        target['pixel_decl_sha256'] = pixel_declaration_sha256(4, 4)
        target['rgb'][0, 0] = 99
        with self.assertRaisesRegex(ManifestError, 'raw frame digest'):
            _build(cases, lanes)

    def test_bool_integer_fields_duplicate_json_keys_and_closed_records(self):
        baseline = _build()
        for name, mutate in (
            ('master_seed', {'master_seed': True}),
            ('generator_sha256', {'generator_sha256': 'z' * 64}),
        ):
            with self.subTest(name=name), self.assertRaises(ManifestError):
                _build(**mutate)
        cases, lanes = _corpus()
        cases[0]['seed_tag'] = False
        with self.assertRaises(ManifestError):
            _build(cases, lanes)
        cases, lanes = _corpus()
        cases[0]['extra'] = 'text'
        with self.assertRaises(ManifestError):
            _build(cases, lanes)
        with self.assertRaisesRegex(ManifestError, 'duplicate JSON key'):
            parse_metadata('{"schema":1,"schema":2}')
        with self.assertRaisesRegex(ManifestError, 'float forbidden'):
            parse_metadata('{"seed":1.5}')
        with self.assertRaisesRegex(ManifestError, 'seal mismatch'):
            verify_manifest(baseline.canonical_json,
                            hashlib.sha256(b'unrelated').hexdigest())

    def test_no_caller_mutation_or_pixel_persistence(self):
        cases, lanes = _corpus()
        original = [f['rgb'].copy() for c in cases for f in c['frames']]
        # A write spy is armed to reject persistence without executing a write.
        with patch('builtins.open', side_effect=AssertionError('pixel write forbidden')) as write_spy:
            manifest = _build(cases, lanes)
            self.assertTrue(manifest.sha256)
            write_spy.assert_not_called()
        for frame, before in zip((f['rgb'] for c in cases for f in c['frames']), original):
            np.testing.assert_array_equal(frame, before)
            self.assertTrue(frame.flags.writeable)
        detached = manifest.metadata()
        detached['frames'][0]['raw_sha256'] = '0' * 64
        self.assertNotEqual(detached, manifest.metadata())

    def test_all_author_intent_categories_and_source_neutral_handles(self):
        faults = ('static', 'localized_change', 'integer_translation', 'periodic_flicker',
                  'real_motion_veto', 'sparse_support', 'mixed_history', 'skipped_frame',
                  'resize_epoch', 'caller_mutation', 'plugin_exception', 'eviction')
        cases = [_case('case_' + str(i), label, (0, 3)) for i, label in enumerate(faults)]
        lanes = [{'handle': _B, 'case_ids': [c['case_id'] for c in cases],
                  'epoch_plan': [{'case_id': c['case_id'], 'epoch': i,
                                  'frame_seqs': [0, 3]} for i, c in enumerate(cases)]}]
        doc = _build(cases, lanes).metadata()
        self.assertEqual({c['expected']['label'] for c in doc['cases']}, set(faults))
        self.assertEqual(len(doc['frames']), 2*len(faults))
        # Changing one lane's case content cannot silently reproduce the same seal.
        cases[0]['frames'][0]['rgb'][0, 0] = 255
        with self.assertRaises(ManifestError):
            _build(cases, lanes)


if __name__ == '__main__':
    unittest.main()
