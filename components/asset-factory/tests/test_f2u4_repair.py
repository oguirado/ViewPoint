"""Adversarial review regressions; each defect was probed before repair."""
import copy
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import test_f2u4_glb as fixtures
from tools.blender import glb_io as io, glb_capture as capture, helpers_contracts as contracts
from tools.blender.helpers_contracts import canonical, digest, json_digest


class RepairTests(fixtures.GLBTests):
    def gate(self, name='gate.json', rc=0, **extra):
        value = {'schema': 'af.mesh-regression/v1', 'rc': rc, 'status': contracts.STATUS[rc],
                 'verdict': contracts.VERDICT[rc], 'input_sha256': digest(self.data), **extra}
        path = self.root/name
        path.write_bytes(canonical(value))
        return {'path': name, 'sha256': digest(path.read_bytes()), 'subject_sha256': digest(self.data)}

    def frames(self):
        return {'schema': 'af.glb-capture-frames/v1', 'helpers_declared': True, 'source_sha256': digest(self.data),
                'candidate_sha256': digest(self.data), 'profile': copy.deepcopy(capture.CAPTURE_PROFILE),
                'views': [{'view': i, 'raw_visual_rgb_sha256': 'a'*64,
                           'candidate_rgb_sha256': 'a'*64, 'diagnostic_rgb_sha256': 'b'*64,
                           'background_rgb_sha256': 'd'*64,
                           'pixel_count': 4096} for i in range(capture.CAPTURE_PROFILE['views'])]}

    def evaluate(self, value):
        return capture.evaluate_captures(value, source_sha256=digest(self.data), candidate_sha256=digest(self.data))

    def test_f1_blank_frames_cannot_pass(self):
        value = self.frames()
        for v in value['views']:
            v['diagnostic_rgb_sha256'] = v['raw_visual_rgb_sha256']
        self.assertEqual(self.evaluate(value)['rc'], 3)

    def test_f1_capture_has_background_and_lower_views(self):
        source = inspect.getsource(capture._blender_main)
        self.assertIn('clip_start', source)
        self.assertIn('clip_end', source)
        self.assertIn('background_rgb_sha256', source)
        self.assertGreaterEqual(capture.CAPTURE_PROFILE['views'], 6)

    def test_f1_empty_background_is_not_asset_evidence(self):
        value = self.frames()
        for v in value['views']: v['background_rgb_sha256'] = v['raw_visual_rgb_sha256']
        self.assertEqual(self.evaluate(value)['rc'], 3)

    def test_f1_invisible_helper_still_fails_partition(self):
        self.doc['nodes'][3]['scale'] = [0.000001]*3; self.save()
        result = capture.compare_visuals(self.source, self.source, roles=self.roles, timeout_s=30)
        self.assertEqual(result['rc'], 5)

    def test_f2_unknown_gate_schema_refuses(self):
        self.metadata['gate_refs'] = [self.gate(schema='unknown/v1')]
        self.assertEqual(self.separate()['rc'], 2)

    def test_f2_stage_ok_is_not_gate_pass(self):
        ref = self.gate()
        path = self.root/ref['path']
        value = json.loads(path.read_text()); value.pop('verdict')
        path.write_bytes(canonical(value)); ref['sha256'] = digest(path.read_bytes())
        self.metadata['gate_refs'] = [ref]
        self.assertEqual(self.separate()['rc'], 2)

    def test_f2_required_coverage_is_explicit(self):
        self.assertIn('required_gates', inspect.signature(io.separate_glb).parameters)
        self.assertEqual(self.separate(required_gates=())['rc'], 3)
        self.assertEqual(self.separate(required_lod_levels=(9,))['rc'], 3)

    def test_f3_mutants_change_internal_rules(self):
        source = Path(fixtures.__file__).with_name('test_f2u4_mutants.py').read_text()
        self.assertNotIn("'separate_glb', side_effect=", source)
        self.assertNotIn("'verify_bundle', return_value=", source)
        self.assertIn('mutate_rule', source)

    def test_f4_reaping_witness_has_deadlines(self):
        source = inspect.getsource(fixtures.GLBTests.test_u4_timeout_reaps_owned_processes)
        self.assertIn('monotonic', source)
        self.assertIn('assertLess', source)
        self.assertNotIn('if stat.exists(): self.assertEqual', source)

    def test_f5_bundle_snapshots_gate_evidence(self):
        ref = self.gate()
        self.metadata['gate_refs'] = [ref]
        result = self.separate()
        self.assertEqual(result['rc'], 0, result)
        stored = self.target/result['gate_refs'][0]['path']
        self.assertTrue(stored.is_file(), stored)
        (self.root/ref['path']).unlink()
        self.assertEqual(io.verify_bundle(self.target)['rc'], 0)
        stored.write_bytes(b'tampered')
        self.assertEqual(io.verify_bundle(self.target)['rc'], 4)

    def test_f5_schemas_use_frozen_subset(self):
        from tools.blender import glb_schemas
        def walk(value):
            if isinstance(value, dict):
                self.assertNotIn('pattern', value)
                self.assertNotIn('maxItems', value)
                for v in value.values(): walk(v)
            elif isinstance(value, list):
                for v in value: walk(v)
        for s in (glb_schemas.SEPARATION_SCHEMA, glb_schemas.RECEIPT_SCHEMA, glb_schemas.CAPTURE_SCHEMA): walk(s)

    def test_f5_lod_snapshot_is_independent_and_rehashed(self):
        doc = copy.deepcopy(self.doc); doc['nodes'][1]['name'] = 'distinct-lod'
        content = io.encode_glb(doc, self.binary)
        lod = self.root/'lod.glb'; lod.write_bytes(content)
        self.metadata['lods'] = [{'level': 7, 'path': lod.name, 'sha256': digest(content)}]
        gate = self.gate(candidate_sha256=digest(content)); gate['subject_sha256'] = digest(content)
        self.metadata['gate_refs'] = [gate]
        result = self.separate(required_lod_levels=(7,))
        self.assertEqual(result['rc'], 0, result)
        lod.unlink(); (self.root/gate['path']).unlink()
        self.assertEqual(io.verify_bundle(self.target)['rc'], 0)
        stored = self.target/result['lods'][0]['path']; stored.write_bytes(b'changed')
        self.assertEqual(io.verify_bundle(self.target)['rc'], 4)

    def test_f6_capture_is_a_bound_closed_gate(self):
        value = self.evaluate(self.frames())
        self.assertEqual(value.get('schema'), 'af.glb-capture/v1')
        self.assertEqual(value.get('subject_sha256'), digest(self.data))
        self.assertIn('expected_source_sha256', inspect.signature(capture.compare_visuals).parameters)
        self.assertIn('receipt_path', inspect.signature(capture.compare_visuals).parameters)

    def test_f6_stale_pin_preserves_caller_receipt(self):
        output = self.root/'capture.json'; output.write_bytes(b'CALLER')
        result = capture.compare_visuals(self.source, self.source, roles=self.roles, timeout_s=30,
                                        expected_source_sha256='0'*64, receipt_path=output)
        self.assertEqual(result['rc'], 4)
        self.assertEqual(result['subject_sha256'], digest(self.data))
        self.assertEqual(output.read_bytes(), b'CALLER')

    def test_f6_capture_publication_and_large_asset_visibility(self):
        self.doc, self.binary, self.roles = fixtures.fixture(False)
        self.doc['nodes'][0]['scale'] = [10000,10000,10000]; self.save()
        self.assertEqual(self.separate()['rc'], 0)
        candidate = self.target/'visual.glb'; output = self.target/'capture.json'
        result = capture.compare_visuals(self.source, candidate, roles=self.roles, timeout_s=90,
                                        expected_source_sha256=digest(self.data),
                                        expected_candidate_sha256=digest(candidate.read_bytes()),
                                        expected_receipt_sha256=digest((self.target/'receipt.json').read_bytes()),
                                        receipt_path=output)
        self.assertEqual(result['rc'], 0, result)
        self.assertEqual(json.loads(output.read_text()), result)
        self.assertEqual(output.read_bytes(), canonical(result))
        self.assertTrue(all(v['raw_visual_rgb_sha256'] != v['background_rgb_sha256'] for v in result['captures']['views']))
        self.assertTrue(result['renderer']['version'].startswith('Blender '))
        from tools.blender.glb_schemas import CAPTURE_SCHEMA
        contracts.core.validate(result, CAPTURE_SCHEMA)
        reference = {'gate': 'glb-capture', 'schema': result['schema'], 'path': output.name,
                     'sha256': digest(output.read_bytes()), 'subject_sha256': digest(candidate.read_bytes()),
                     'verdict': result['verdict']}
        self.assertEqual(reference['subject_sha256'], result['subject_sha256'])
        self.assertEqual(io.verify_bundle(self.target)['rc'], 0)

    def test_f6_output_never_replaces_source(self):
        self.doc, self.binary, self.roles = fixtures.fixture(False); self.save()
        result = capture.compare_visuals(self.source, self.source, roles=self.roles, timeout_s=30,
                                        receipt_path=self.source)
        self.assertEqual(result['rc'], 2)
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_f7_upstream_precedence_is_order_independent(self):
        self.metadata['gate_refs'] = [self.gate('incomplete.json', 3), self.gate('failed.json', 5)]
        self.assertEqual(self.separate()['rc'], 5)
        self.metadata['gate_refs'].reverse()
        self.assertEqual(self.separate()['rc'], 5)

    def test_f8_overflow_is_error_without_traceback(self):
        self.assertEqual(self.separate()['rc'], 0)
        report_path = self.target/'separation.json'
        report = json.loads(report_path.read_text())
        report['nodes'][0]['world_matrix_hex'][0] = '0x1p+5000'
        report_path.write_bytes(canonical(report))
        receipt_path = self.target/'receipt.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['report']['sha256'] = digest(report_path.read_bytes())
        receipt_path.write_bytes(canonical(receipt))
        self.assertEqual(io.verify_bundle(self.target)['rc'], 2)

    def test_f8_invalid_capture_pin_is_structured_error(self):
        result = capture.evaluate_captures(None, source_sha256=False, candidate_sha256='invalid')
        self.assertEqual(result['rc'], 2)
        self.assertIsNone(result['source_sha256'])
        self.assertIsNone(result['candidate_sha256'])

    def test_f9_kernel_is_imported_or_stubbed(self):
        self.assertTrue(hasattr(contracts, 'core'))
        with mock.patch.object(contracts.core, 'canonical_json', wraps=contracts.core.canonical_json) as call:
            self.assertEqual(contracts.canonical({'x': 2}), b'{"x":2}\n')
            self.assertEqual(call.call_count, 1)

    def test_f10_lod_fixture_distinguishes_candidate(self):
        source = inspect.getsource(fixtures.GLBTests.test_u4_missing_lod_is_not_renumbered)
        self.assertNotIn('lod.write_bytes(self.data)', source)
        self.assertNotIn("mock.patch.object(io.time, 'monotonic'", inspect.getsource(fixtures.GLBTests))

    def test_f11_special_file_read_does_not_block(self):
        fifo = self.root/'input.fifo'; os.mkfifo(fifo)
        code = 'from tools.blender.helpers_contracts import read_bytes; read_bytes(__import__("sys").argv[1])'
        try:
            proc = subprocess.run([sys.executable, '-B', '-c', code, str(fifo)], timeout=2,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except subprocess.TimeoutExpired:
            self.fail('FIFO read blocked past deadline')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b'non_regular_input', proc.stderr)

    def test_f11_renderer_is_memory_scoped(self):
        with mock.patch.object(capture.subprocess, 'Popen') as popen, mock.patch.object(capture.os, 'waitid', return_value=object()), mock.patch.object(capture.os, 'killpg'):
            popen.return_value.wait.return_value = 0
            popen.return_value.returncode = 0
            popen.return_value.stderr.read.return_value = b''
            popen.return_value.pid = 999999999
            capture.run_owned(['blender', '-b'], timeout_s=1, cwd=self.root)
        self.assertIn('MemoryMax=8G', popen.call_args.args[0])
        self.assertIn('systemd-run', popen.call_args.args[0])

    def test_f11_renderer_failure_preserves_diagnostics(self):
        with self.assertRaises(contracts.GlbError) as error:
            capture.run_owned([sys.executable, '-c', 'import sys;sys.stderr.write("diagnostic-marker");sys.exit(2)'],
                              timeout_s=5, cwd=self.root)
        self.assertEqual(error.exception.rc, 2)
        self.assertIn('diagnostic-marker', error.exception.diagnostics)

    def test_f11_capture_refusal_exposes_renderer_diagnostics(self):
        self.doc, self.binary, self.roles = fixtures.fixture(False); self.save()
        error = contracts.GlbError('renderer_failed'); error.diagnostics = 'diagnostic-marker'
        with mock.patch.object(capture, 'run_owned', side_effect=error):
            result = capture.compare_visuals(self.source, self.source, roles=self.roles, timeout_s=30)
        self.assertEqual(result['rc'], 2)
        self.assertEqual(result.get('diagnostics'), 'diagnostic-marker')

    def test_f11_evidence_count_is_bounded(self):
        self.metadata['gate_refs'] *= io.MAX_EVIDENCE_FILES + 1
        self.assertEqual(self.separate()['rc'], 6)

    def test_f12_render_fixture_does_not_inherit_tests(self):
        self.assertNotIn(fixtures.GLBTests, fixtures.GLBRenderTests.__bases__)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(RepairTests(n) for n in loader.getTestCaseNames(RepairTests) if n.startswith('test_f'))
