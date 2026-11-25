"""Prove the independent schema-surface checks reject planted defects."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from textwrap import indent
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class SchemaControlTests(unittest.TestCase):
    def test_surface_oracles_detect_planted_defects(self):
        spec = importlib.util.spec_from_file_location('f0u1_surface_oracle', ROOT / 'tests' / 'test_f0u1_contracts.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        originals = {p.name: json.loads(p.read_text()) for p in (ROOT / 'slots').glob('*.schema.json')}
        controls = ('baseline', 'wrong_id', 'missing_schema', 'external_ref',
                    'unsupported_keyword', 'open_envelope', 'changed_media_comment',
                    'owner_external_ref')
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            (scratch / 'slots').mkdir()
            for control in controls:
                with self.subTest(control=control):
                    docs = copy.deepcopy(originals)
                    if control == 'wrong_id': docs['generate.schema.json']['$id'] = 'urn:af:wrong:v1'
                    if control == 'missing_schema': del docs['provider.schema.json']
                    if control == 'external_ref': docs['generate.schema.json']['oneOf'][0]['$ref'] = 'https://example.invalid/schema'
                    if control == 'unsupported_keyword': docs['generate.schema.json']['not'] = {}
                    if control == 'open_envelope': docs['generate.schema.json']['$defs']['request']['additionalProperties'] = True
                    if control == 'changed_media_comment':
                        docs['audio.schema.json']['$comment'] = 'changed media contract'
                    if control == 'owner_external_ref':
                        docs['storyboard.schema.json']['allOf'].append(
                            {'$ref': 'https://example.invalid/schema'})
                    for p in (scratch / 'slots').iterdir(): p.unlink()
                    for name, doc in docs.items():
                        (scratch / 'slots' / name).write_text(json.dumps(doc))
                    suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.SchemaSurfaceTests)
                    transcript = io.StringIO()
                    with mock.patch.object(module, 'ROOT', scratch):
                        result = unittest.TextTestRunner(stream=transcript).run(suite)
                    self.assertEqual(result.testsRun, 2)
                    # Preserve the parent runner's single unindented unittest summary.
                    diagnostic = "\n" + indent(transcript.getvalue(), "    ")
                    self.assertEqual(result.wasSuccessful(), control == 'baseline', diagnostic)
                    if control != 'baseline':
                        self.assertTrue(result.failures or result.errors, diagnostic)
                    if control == 'changed_media_comment':
                        self.assertEqual(len(result.failures), 1, diagnostic)
                        self.assertIn('test_schema_inventory_and_identity',
                                      result.failures[0][0].id(), diagnostic)
                    if control == 'owner_external_ref':
                        self.assertTrue(any('test_local_refs_closed_objects_and_subset' in test.id()
                                            and 'https://example.invalid/schema' in failure
                                            for test, failure in result.failures), diagnostic)
                    print('F0-U1 schema control:', control, 'GREEN' if result.wasSuccessful() else 'RED')
