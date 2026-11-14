"""JDOCS-6/7/8 regression oracles; mutations stay in disposable copies."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools import build_catalogue, route_code

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hyperbooks' / '_tools'))
import admission


class SealDocs(unittest.TestCase):
    def temporary(self):
        scratch = ROOT / 'build' / 'room_FB'
        scratch.mkdir(parents=True, exist_ok=True)
        ctx = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(ctx.cleanup)
        return Path(ctx.name)

    def test_gui_live_anchor_routes_and_removal_is_detected(self):
        # Scan actual source, rather than inventing a synthetic GUI symbol.
        layers = json.loads((ROOT / 'catalogue/layers.src').read_text())
        rows = build_catalogue.scan_symbols(str(ROOT), layers)
        build_catalogue.sym_docs(str(ROOT), rows)
        gui = [r for r in rows if r['anchor'] == 'va.gui.frames']
        self.assertEqual(len(gui), 1)
        self.assertEqual(gui[0]['path'], 'vadapt/gui/from_screens.py')
        self.assertIn('screenshot', gui[0]['doc'].lower())
        scratch = self.temporary()
        shutil.copytree(ROOT / 'catalogue', scratch / 'catalogue')
        symbols = scratch / 'catalogue/symbols.jsonl'
        symbols.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        question = 'Where are screenshots collected into the GUI frame manifest?'
        result = route_code.route(question, 3, str(scratch))
        self.assertIn('va.gui.frames', [r[0] for r in result['r']], result)
        # A deleted source anchor must invalidate the projected catalogue.
        shutil.copytree(ROOT / 'vadapt', scratch / 'vadapt', ignore=shutil.ignore_patterns('__pycache__'))
        source = scratch / gui[0]['path']
        source.write_text(source.read_text().replace('# @anchor:va.gui.frames', '# removed GUI anchor'))
        mutated = build_catalogue.scan_symbols(str(scratch), layers)
        self.assertNotIn('va.gui.frames', [r['anchor'] for r in mutated])
        self.assertNotEqual([r for r in mutated if r['path'] == gui[0]['path']],
                            [r for r in rows if r['path'] == gui[0]['path']])

    def test_dirty_git_source_hash_rejected_for_bound_and_unbound_receipts(self):
        scratch = self.temporary()
        def git(*args):
            subprocess.run(['git', *args], cwd=scratch, check=True, capture_output=True, timeout=20)
        git('init', '-q')
        src = scratch / 'src.py'
        src.write_text('value = 1\n')
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        git('add', 'src.py')
        receipt = dict(schema='hb.receipt.v1', id='R-test', kind='test', argv=[], cwd='.',
                       commit=None, tree_clean=False, sources={'src.py': digest}, exit_code=0,
                       output_sha256='0' * 64, metrics={})
        path = scratch / 'receipt.json'
        src.write_text('value = 2\n')
        self.assertIn('src.py', admission._dirty(str(scratch)))
        for commit in (None, 'a' * 40):
            with self.subTest(commit=commit):
                receipt.update(commit=commit, tree_clean=commit is not None)
                path.write_text(json.dumps(receipt))
                for dirty in (None, set(), {'src.py'}):
                    self.assertTrue(any('hash drift' in x for x in
                                        admission.check_receipt(str(path), str(scratch), dirty)))
        src.write_text('value = 1\n')
        self.assertEqual(admission.check_receipt(str(path), str(scratch)), [])

    def test_required_artifact_deletion_malformed_and_whole_book(self):
        scratch = self.temporary()
        hb = scratch / 'hyperbooks'
        shutil.copytree(ROOT / 'hyperbooks', hb, ignore=shutil.ignore_patterns('__pycache__'))
        declaration = json.loads((hb / 'COMPANION_FORMAT.json').read_text())
        books = sorted(p.name for p in hb.glob('HB-VC-*') if p.is_dir())
        self.assertTrue(books)
        for book in books:
            for name in declaration['required_per_book']:
                target = hb / book / name
                original = target.read_bytes()
                for bad in (None, b'malformed artifact\n'):
                    with self.subTest(book=book, artifact=name, bad=bad):
                        if bad is None:
                            target.unlink()
                        else:
                            target.write_bytes(bad)
                        defects = admission.check_corpus(str(hb), str(ROOT))
                        self.assertTrue(any(book in x and name in x for x in defects), defects)
                        target.write_bytes(original)
            (hb / book).rename(hb / ('removed-' + book))
            defects = admission.check_corpus(str(hb), str(ROOT))
            self.assertTrue(any(book in x for x in defects), defects)
            (hb / ('removed-' + book)).rename(hb / book)
