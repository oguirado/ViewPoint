"""W9 integration: receipts remain binding and decoder exceptions fail closed."""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('w9_gates', ROOT / 'tools/gates.py')
gates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gates)


class IntegrationGates(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'build/room_I'
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def put(self, path, content):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def boundary(self, decoder, importer='blender_visual_qa/truth/decode_worker.py'):
        self.put('bpy_side/q.py', '')
        self.put('bpy_side/canon.py', '')
        self.put('bpy_side/decode_exr.py', decoder)
        self.put(importer, 'from bpy_side import decode_exr as dx\n')
        return gates.g_bpy_boundary(self.root)

    def test_pure_decoder_only_for_decode_worker(self):
        self.assertEqual(self.boundary('import numpy\n'), [])
        self.assertTrue(self.boundary('import numpy\n', 'blender_visual_qa/truth/other.py'))

    def test_decoder_bpy_import_rejected_even_when_lazy(self):
        for code in ('import bpy\n', 'def load():\n    import bpy\n',
                     'import importlib\nimportlib.import_module("bpy")\n',
                     'from bpy_side import passes\n',
                     'name = \"bpy\"\n__import__(name)\n', 'from . import passes\n'):
            with self.subTest(code=code):
                self.assertTrue(self.boundary(code))

    def test_readoption_preserves_original_and_current_hash(self):
        old, new = gates.digest(b'old'), gates.digest(b'new')
        self.put('tools/MIGRATION_SOURCE.json', '{"repo":"fixture","commit":"fixture"}')
        self.put('tools/MIGRATION_POLICY.json', '{}')
        self.put('bin/blender-visual-qa', 'entry')
        self.put('migration/copy_manifest.tsv', 'reference')
        self.put('adopt.py', 'new')
        receipt = {'path':'adopt.py', 'sha256':new, 'migration_sha256':old,
                   'readoption':{'old_sha256':old, 'new_sha256':new, 'wave':'W9'}}
        self.put('tools/ADOPTED.json', json.dumps({'files':[receipt]}))
        with (self.root / 'copy_manifest.tsv').open('w') as f:
            w = csv.DictWriter(f, fieldnames=['src','dst','action','note'], delimiter='\t')
            w.writeheader()
            w.writerow({'src':'@adopt/adopt.py','dst':'adopt.py','action':'ADOPT','note':json.dumps({'sha256':old})})
        with patch.object(gates, 'git', return_value=b''):
            self.assertEqual(gates.g_migration(self.root), [])
            self.put('adopt.py', 'drift')
            self.assertIn('adopt.py:adopt_mutated', gates.g_migration(self.root))
            self.put('adopt.py', 'new')
            receipt['readoption']['old_sha256'] = new
            self.put('tools/ADOPTED.json', json.dumps({'files':[receipt]}))
            self.assertTrue(gates.g_migration(self.root))

    def test_pin_inventory_and_wheel_bytes_remain_binding(self):
        cfg = json.loads((ROOT / 'config/vision.json').read_text())
        wheel = Path(cfg['engine_path'])
        wheel = wheel if wheel.is_absolute() else ROOT / wheel
        self.put('tools/VENDORED.json', (ROOT / 'tools/VENDORED.json').read_text())
        cfg['engine_path'] = str(wheel)
        self.put('config/vision.json', json.dumps(cfg))
        self.assertEqual(gates.g_pin(self.root), [])
        cfg['tooling_commit'] = 'invalid'
        self.put('config/vision.json', json.dumps(cfg))
        self.assertIn('commit_mismatch', gates.g_pin(self.root))
        cfg['tooling_commit'] = json.loads((ROOT / 'config/vision.json').read_text())['tooling_commit']
        changed = self.root / 'changed.whl'
        changed.write_bytes(wheel.read_bytes() + b'drift')
        cfg['engine_path'] = str(changed)
        self.put('config/vision.json', json.dumps(cfg))
        self.assertIn('wheel_content_mismatch', gates.g_pin(self.root))
