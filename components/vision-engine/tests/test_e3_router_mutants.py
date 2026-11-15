"""RVMEM-7: mutants are admitted only through executed green-control oracles."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class TestMutantSuite(unittest.TestCase):
    def test_all_mutants_killed(self):
        root = Path(__file__).resolve().parents[1]
        run = root / 'tests' / 'mutants' / 'vmem' / 'run.py'
        p = subprocess.run([sys.executable, str(run)], cwd=root,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(p.returncode, 0, p.stdout[-4000:] + p.stderr[-2000:])
        summary = json.loads(p.stdout.splitlines()[0])
        receipt = json.loads(Path(summary['receipt']).read_text())
        self.assertGreater(receipt['control_checks'], 0)
        self.assertEqual(receipt['killed'], receipt['total'])
        self.assertTrue(all(r['loaded'] and r['checks'] > 0 and r['fails'] > 0 for r in receipt['results']))
        ids = {r['id'] for r in receipt['results']}
        self.assertTrue({'M-VM%d' % n for n in range(1,13)} <= ids)
        modules = ('router','units','compact','receipts','cli','store','gen','blobs','tiers','ingest','gates')
        for name in modules:
            self.assertGreaterEqual(sum(r['module'] == name for r in receipt['results']), 3, name)
