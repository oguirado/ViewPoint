"""Predicate-based router tests with negative fixtures and positive controls."""
import ast
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import build_code_index as b, route_code as r

ROOT = Path(__file__).resolve().parents[1]


def _invoke(main, args):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(args)
    raw = out.getvalue().encode()
    return rc, json.loads(raw) if raw else None, raw


def _fixture(root):
    module = root / 'wender/pkg/mod.py'
    module.parent.mkdir(parents=True)
    module.write_text('# @module {"id":"wa.pkg.mod","layer":0,"owner":"PERCEIVE","book":"HB-WA-s1-perceive"}\n'
                      '# @anchor:wa.pkg.mod.pub_fn\n'
                      'def pub_fn():\n    """Search controls in accessibility tree."""\n    return _helper()\n'
                      '# @anchor:wa.pkg.mod.PubClass\nclass PubClass:\n    pass\n'
                      'def _helper():\n    return 1\n')
    js = root / 'js/inpage/mod.js'
    js.parent.mkdir(parents=True)
    js.write_text('// @module {"id":"wa.js.mod","layer":0,"owner":"PERCEIVE"}\n'
                  '// @anchor:wa.js.mod.walk\n// Walk composed accessibility tree.\nfunction walk() { return 1; }\n')
    hb = root / 'hyperbooks'
    hb.mkdir()
    (hb / 'COMPANION_FORMAT.json').write_text(json.dumps({'expected_books': ['HB-WA-s1-perceive']}))
    book = hb / 'HB-WA-s1-perceive'
    book.mkdir()
    (book / 'book.md').write_text('# Perception\n\n## <a id="hb-s0"></a>0. Scope\n'
                                '<!-- HB:BEGIN hb.section.v2 -->\n```json\n'
                                '{"anchor":"hb-s0","code_anchors":["wa.pkg.mod.pub_fn"],'
                                '"kind":"reading","title":"Perception tree"}\n```\n<!-- HB:END -->\n'
                                'Accessibility controls reference ML-1.\n')
    (book / 'mathlinks.jsonl').write_text(json.dumps({'target_id': 'ML-1', 'target_path': '/home/example/external.md',
                                                    'claimed_location': 'line 1', 'note': 'tree controls', 'status': 'CITED'}) + '\n')
    (book / 'manifest.json').write_text('{}\n')
    return module


def _bad(value):
    raise AssertionError('float on wire')


# @anchor:wa.tools.test_route_code_unit.RouteCodeTests
class RouteCodeTests(unittest.TestCase):
    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.setUpClass
    @classmethod
    def setUpClass(cls):
        cls.ix = b.build_memory_index(ROOT)

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_check_positive_control_clean_repo
    def test_check_positive_control_clean_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture(root)
            for main in (b.main, r.main):
                rc, env, _ = _invoke(main, ['--root', tmp, '--check'])
                self.assertEqual((rc, env['ok'], env['missing_count'], env['missing']), (0, True, 0, []))
        # Only new tool files are in this task's strict edit scope.
        missing = b.check_missing_anchors(ROOT)
        self.assertFalse([x for x in missing if x['file'] in ('tools/build_code_index.py', 'tools/route_code.py')])

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_check_must_fail_unanchored_public_function
    def test_check_must_fail_unanchored_public_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = _fixture(root)
            with module.open('a') as stream:
                stream.write('\ndef unanchored_public_fn(x):\n    return x + 1\n')
            before = module.read_bytes()
            for main in (b.main, r.main):
                rc, env, _ = _invoke(main, ['--root', tmp, '--check'])
                self.assertEqual((rc, env['ok'], env['code']), (4, False, 'E_MISSING_ANCHORS'))
                self.assertEqual(env['missing'][0]['symbol'], 'unanchored_public_fn')
                self.assertEqual(module.read_bytes(), before)
        report, rc = r.check_anchors_mode(ROOT)
        self.assertEqual(rc, 4)
        self.assertTrue(any(x['file'] == 'tools/reseal.py' and x['symbol'] == 'dump' for x in report['missing']))

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_decorators_async_constants_and_comment_spoof
    def test_decorators_async_constants_and_comment_spoof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = _fixture(root)
            with module.open('a') as stream:
                stream.write('\n# @anchor:wa.pkg.mod.dec\n@staticmethod\nasync def dec():\n    """Decorated control."""\n    return pub_fn()\n'
                             '# @anchor:wa.pkg.mod.VALUE\nVALUE = 2\n')
            ix = b.build_memory_index(root)
            row = next(x for x in ix['anchors'] if x['id'] == 'wa.pkg.mod.dec')
            self.assertEqual((row['symbol'], row['kind']), ('dec', 'function'))
            self.assertIn('wa.pkg.mod.pub_fn', row['callees'])
            self.assertEqual(b.check_missing_anchors(root), [])
            with module.open('a') as stream:
                stream.write('\n"""\n# @anchor:wa.pkg.mod.spoof\n"""\ndef spoof():\n    return 0\n')
            self.assertTrue(any(x['symbol'] == 'spoof' for x in b.check_missing_anchors(root)))
            self.assertNotIn('wa.pkg.mod.spoof', [x['id'] for x in b.build_memory_index(root)['anchors']])

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_missing_header_and_js_public
    def test_missing_header_and_js_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = _fixture(root)
            module.write_text(module.read_text().replace('# @module ', '# invalid ', 1))
            js = root / 'js/inpage/mod.js'
            js.write_text(js.read_text() + '\nfunction unanchored() {}\n')
            missing = b.check_missing_anchors(root)
            self.assertEqual({x['kind'] for x in missing}, {'module_header', 'js_function'})

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_build_code_index_deterministic_and_callgraph
    def test_build_code_index_deterministic_and_callgraph(self):
        with tempfile.TemporaryDirectory() as tmp:
            out1, out2 = Path(tmp) / 'ix1', Path(tmp) / 'ix2'
            b.build(ROOT, out1)
            original = b.source_files
            with patch.object(b, 'source_files', side_effect=lambda root: list(reversed(original(root)))):
                b.build(ROOT, out2)
            tree = lambda out: {p.name: p.read_bytes() for p in out.iterdir()}
            self.assertEqual(tree(out1), tree(out2))
            self.assertEqual(r.load_index(ROOT, out1)['anchors'], self.ix['anchors'])
            self.assertEqual(_invoke(b.main, ['--root', str(ROOT), '--out', str(out1), '--check-index'])[0], 0)
        man = self.ix['manifest']
        self.assertGreaterEqual(man['nodes'], 440)
        self.assertEqual(man['hb_books'], 5)
        self.assertLessEqual(man['max_file_bytes'], b.CAP)
        rows = {x['id']: x for x in self.ix['anchors']}
        self.assertIn('wa.tools.layer_check.check', rows['wa.tools.layer_check.main']['callees'])
        self.assertIn('wa.tools.layer_check.main', rows['wa.tools.layer_check.check']['callers'])

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_duplicate_anchor_must_fail
    def test_duplicate_anchor_must_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = _fixture(root)
            self.assertTrue(b.build_memory_index(root)['anchors'])
            module.write_text(module.read_text() + '\n# @anchor:wa.pkg.mod.pub_fn\nVALUE = 1\n')
            with self.assertRaisesRegex(b.BuildError, 'E_DUPLICATE_ANCHOR'):
                b.build_memory_index(root)

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_route_query_bm25_and_tiebreaks
    def test_route_query_bm25_and_tiebreaks(self):
        cases = [('prune accessibility tree cdp', {'wa.perceive.ax.prune'}),
                 ('canonical state json validate build_state', {'wa.state.schema.build_state', 'wa.state.schema.validate_state'}),
                 ('single actor lock choose_path', {'wa.act.actor.ActorLock', 'wa.act.actor.choose_path'}),
                 ('reproduced share ppm demonstrations', {'wa.learn.demonstrations.reproduced_share_ppm'})]
        for query, expected in cases:
            ranked, qt = r.rank_query(self.ix, query)
            self.assertTrue(expected & {d['id'] for d in ranked[:3]})
            self.assertTrue(r.why_for(self.ix, ranked[0], qt))
            self.assertGreater(ranked[0]['score'], 0)
        self.assertEqual(r.rank_query(self.ix, cases[-1][0])[0][0]['id'], next(iter(cases[-1][1])))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = _fixture(root)
            module.write_text(module.read_text() + '\n# @anchor:wa.pkg.m.beta\ndef beta():\n    """Quux identical."""\n    pass\n'
                              '\n# @anchor:wa.pkg.m.alpha\ndef alpha():\n    """Quux identical."""\n    pass\n')
            ranked, _ = r.rank_query(b.build_memory_index(root), 'quux')
            self.assertEqual([d['id'] for d in ranked], ['wa.pkg.m.alpha', 'wa.pkg.m.beta'])
            self.assertEqual(ranked[0]['score'], ranked[1]['score'])
            self.assertEqual(r.rank_query(b.build_memory_index(root), 'quux', 'act')[0], [])

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_anchor_exact_lookup_and_stage_listing
    def test_anchor_exact_lookup_and_stage_listing(self):
        env, rc = r.lookup_anchor(self.ix, 'wa.perceive.ax.prune')
        row = env['r'][0]
        self.assertEqual((rc, row['file'], row['symbol'], row['stage']),
                         (0, 'wender/perceive/ax.py', 'prune', 'HB-WA-s1-perceive'))
        actual = (ROOT / row['file']).read_text().splitlines()[row['line'] - 1]
        self.assertEqual(actual.strip(), '# @anchor:' + row['id'])
        self.assertEqual(row['hb_sections'], ['HB-WA-a0-arch#hb-s2', 'HB-WA-s1-perceive#hb-s0'])
        env, rc = r.lookup_anchor(self.ix, 'wa.nonexistent.symbol')
        self.assertEqual((rc, env['abstain']['reason'], env['r']), (3, 'anchor_not_found', []))
        env, rc = r.list_stage(self.ix, 'perceive')
        self.assertEqual((env, rc), r.list_stage(self.ix, 'HB-WA-s1-perceive'))
        self.assertTrue(env['r'])
        self.assertTrue(all(row['stage'] == 'HB-WA-s1-perceive' for row in env['r']))
        self.assertTrue(env['hb_sections'])
        self.assertEqual(r.list_stage(self.ix, 'unknown_stage')[1], 3)
        self.assertEqual(r.list_stage(self.ix, 'perceive#absent')[1], 3)
        self.assertEqual(r.route_query(self.ix, 'prune', stage_filter='unknown')[1], 3)
        env, rc = r.list_stage(self.ix, 'perceive#hb-s0')
        self.assertEqual(rc, 0)
        self.assertTrue(all('HB-WA-s1-perceive#hb-s0' in next(d for d in self.ix['anchors'] if d['id'] == row['id'])['hb_sections'] for row in env['r']))

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_budget_nofloat_privacy_and_abstain
    def test_budget_nofloat_privacy_and_abstain(self):
        for q, reason in [('zzzznonexistenttoken999', 'no_candidate'), ('the a', 'no_query_term')]:
            env, rc = r.route_query(self.ix, q)
            self.assertEqual((rc, env['r'], env['abstain']['reason']), (3, [], reason))
        full, rc = r.route_query(self.ix, 'prune accessibility tree cdp')
        tight, rc = r.route_query(self.ix, 'prune accessibility tree cdp', budget=450)
        self.assertLess(len(tight['r']), len(full['r']))
        self.assertTrue(tight['r'])
        self.assertLessEqual(len(b.dumps(tight).encode()), 450)
        with self.assertRaises(r.BudgetError):
            r.route_query(self.ix, 'prune', budget=10)
        rc, _, raw = _invoke(r.main, ['--root', str(ROOT), '--q', 'prune', '--budget', '10'])
        self.assertEqual((rc, raw), (6, b''))
        for raw in list(self.ix['files'].values()) + [b.dumps(full).encode(), b.dumps(tight).encode()]:
            self.assertNotRegex(raw.decode(), r'/home/|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|\b\d{4}-\d\d-\d\dT')
            if len(raw.strip()) == 64 and raw.strip().isalnum():
                continue
            for line in raw.splitlines():
                json.loads(line, parse_float=_bad, parse_constant=_bad)
        with self.assertRaisesRegex(b.BuildError, 'E_FLOAT'):
            b.dumps({'nested': [0.5]})
        self.assertEqual(b.dumps({'n': 1}), '{"n":1}\n')
        self.assertEqual(r.clip('éé', 3), 'é')

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_stale_added_deleted_and_corrupt_index
    def test_stale_added_deleted_and_corrupt_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            module = _fixture(root)
            out = Path(tmp) / 'ix'
            b.build(root, out)
            self.assertTrue(r.load_index(root, out)['anchors'])
            before = module.read_bytes()
            module.write_bytes(before + b'\n# source changed\n')
            with self.assertRaisesRegex(r.IndexError, 'E_STALE'):
                r.load_index(root, out)
            self.assertEqual(_invoke(r.main, ['--root', str(root), '--index', str(out), '--q', 'tree'])[0], 5)
            self.assertEqual(_invoke(b.main, ['--root', str(root), '--out', str(out), '--check-index'])[0], 5)
            module.write_bytes(before)
            added = module.parent / 'added.py'
            added.write_text('# new source\n')
            with self.assertRaisesRegex(r.IndexError, 'E_STALE'):
                r.load_index(root, out)
            added.unlink()
            module.unlink()
            with self.assertRaisesRegex(r.IndexError, 'E_STALE'):
                r.load_index(root, out)
            module.write_bytes(before)
            b.build(root, out)
            (out / 'post.json').write_text('{}\n')
            with self.assertRaisesRegex(r.IndexError, 'E_INDEX'):
                r.load_index(root, out)
            self.assertEqual(_invoke(r.main, ['--root', str(root), '--index', str(out), '--q', 'tree'])[0], 3)

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_stage_fallback_hyperbook_and_privacy
    def test_stage_fallback_hyperbook_and_privacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = _fixture(root)
            module.write_text(module.read_text().replace('HB-WA-s1-perceive', 'HB-WA-s2-perceive'))
            ix = b.build_memory_index(root)
            row = next(d for d in ix['anchors'] if d['id'] == 'wa.pkg.mod.pub_fn')
            self.assertEqual(row['stage'], 'HB-WA-s1-perceive')
            self.assertEqual(row['hb_sections'], ['HB-WA-s1-perceive#hb-s0'])
            self.assertNotIn('/home/', b.dumps(ix['hb']))
            section = ix['hb']['sections'][0]
            self.assertEqual((section['line'], section['mathlinks'][0]['target_id']), (3, 'ML-1'))
            self.assertIn('tree', ix['post'])
            self.assertIn('hb', ix['post']['tree']['z'])
            module.write_text(module.read_text().replace('HB-WA-s2-perceive', 'HB-WA-a0-arch'))
            self.assertEqual(b.build_memory_index(root)['anchors'][-1]['stage'], 'HB-WA-s1-perceive')

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_tokenization_and_integer_formula
    def test_tokenization_and_integer_formula(self):
        tokens = b.toks('ActorLock Ａｎｃｈｏｒｓ build_state pruning isolating isolation')
        for term in ('actor', 'lock', 'anchors', 'anchor', 'state', 'prun', 'prune', 'isolat'):
            self.assertIn(term, tokens)
        self.assertEqual(b.toks('a the x'), [])
        self.assertEqual(b.idf_ppm(1, 1), 287682)
        self.assertIs(type(b.idf_ppm(440, 2)), int)
        ix = {'cols': {'A': {'doc': 1000}, 'B': {'doc': 750000}, 'len': {'doc': [1]}}}
        self.assertEqual(r.zone_x(ix, 'doc', 0, 1, 2000), 2000000)

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_cli_determinism_no_writes_and_usage
    def test_cli_determinism_no_writes_and_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture(root)
            args = ['--root', tmp, '--q', 'tree']
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            outputs = []
            for seed, tz in [('1', 'UTC'), ('714', 'Pacific/Auckland')]:
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONHASHSEED=seed, TZ=tz, SOURCE_DATE_EPOCH=seed)
                result = subprocess.run([sys.executable, str(ROOT / 'tools/route_code.py')] + args,
                                        env=env, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(result.stdout)
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[0], _invoke(r.main, args)[2])
            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            self.assertFalse((root / 'out').exists())
            # Build under perturbed process environments as well as in-process traversal.
            for i, seed in enumerate(('1', '714')):
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONHASHSEED=seed, TZ='UTC' if i == 0 else 'Asia/Tokyo', SOURCE_DATE_EPOCH=seed)
                proc = subprocess.run([sys.executable, str(ROOT / 'tools/build_code_index.py'), '--root', tmp,
                                       '--out', str(root / ('ix' + str(i)))], env=env, capture_output=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual({p.name: p.read_bytes() for p in (root / 'ix0').iterdir()},
                             {p.name: p.read_bytes() for p in (root / 'ix1').iterdir()})
        for args in ([], ['--q', 'tree', '--k', '0'], ['--anchor', 'x', '--q', 'tree']):
            self.assertEqual(_invoke(r.main, args)[0], 2)

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_graph_aliases_and_shadowed_names
    def test_graph_aliases_and_shadowed_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = _fixture(root)
            with module.open('a') as stream:
                stream.write('\n# @anchor:wa.pkg.mod.walk\ndef walk():\n    return 1\n'
                             '# @anchor:wa.pkg.mod.outer\ndef outer():\n'
                             '    def walk():\n        return 2\n    return walk()\n'
                             '# @anchor:wa.pkg.mod.direct\ndef direct():\n    return walk()\n')
            imported = module.parent / 'use.py'
            imported.write_text('# @module {"id":"wa.pkg.use","layer":0,"owner":"PERCEIVE"}\n'
                                'from wender.pkg.mod import pub_fn as alias\nimport wender.pkg.mod as mod\n'
                                '# @anchor:wa.pkg.use.caller\ndef caller():\n    alias()\n    mod.pub_fn()\n')
            rows = {d['id']: d for d in b.build_memory_index(root)['anchors']}
            self.assertEqual(rows['wa.pkg.use.caller']['callees'], ['wa.pkg.mod.pub_fn'])
            self.assertIn('wa.pkg.mod.walk', rows['wa.pkg.mod.direct']['callees'])
            self.assertNotIn('wa.pkg.mod.walk', rows['wa.pkg.mod.outer']['callees'])

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_privacy_doc_redaction_and_invalid_input
    def test_privacy_doc_redaction_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = _fixture(root)
            module.write_text(module.read_text().replace('Search controls in accessibility tree.',
                              'Search /home/example/data user@example.invalid $PRIVATE_ENV 2026-01-01T00:00:00Z sk-fakefixture.'))
            ix = b.build_memory_index(root)
            for raw in ix['files'].values():
                self.assertNotIn(b'/home/', raw)
                self.assertNotIn(b'user@example.invalid', raw)
                self.assertNotIn(b'$PRIVATE_ENV', raw)
                self.assertNotIn(b'sk-fakefixture', raw)
            self.assertTrue(r.route_query(ix, 'search')[0]['r'])
            with self.assertRaises(b.BuildError):
                b.dumps({0.5: 'float key'})
            module.write_text(module.read_text().replace('# @module {', '# @module {bad'))
            self.assertEqual(_invoke(r.main, ['--root', tmp, '--q', 'search'])[0], 3)

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_protected_prefix_packer
    def test_protected_prefix_packer(self):
        make = lambda rows, overflow: {'r': rows, 'overflow': overflow}
        base, full = {'id': 'first'}, {'id': 'first', 'doc': 'x' * 500}
        rows, overflow = r.build_rows([(base, full)], 1, make, 100)
        self.assertEqual((rows, overflow), ([base], False))
        rows, overflow = r.build_rows([(base, full)], 1, make, 35)
        self.assertEqual((rows, overflow), ([], True))
        self.assertLessEqual(len(b.dumps(make(rows, overflow)).encode()), 35)
        with self.assertRaises(r.BudgetError):
            r.build_rows([(base, full)], 1, make, 10)

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_optimized_build_matches_reference
    def test_optimized_build_matches_reference(self):
        # Predicate: every serialized byte agrees with stdlib AST traversal.
        with patch.object(b, '_walk', ast.walk):
            reference = b.build_memory_index(ROOT)
        self.assertEqual(b.build_memory_index(ROOT)['files'], reference['files'])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = _fixture(root)
            # Captured before optimizing; independent of the builder's own source.
            expected = '1f6d454eb85dd92050eb14819d51587376a450d3eab45368c594e7a60716f2b8'
            self.assertEqual(b.build_memory_index(root)['manifest']['index_sha256'], expected)
            # Must-FAIL fixture: a broken tokenizer cannot satisfy that predicate.
            with patch.object(b, 'toks', return_value=[]):
                self.assertNotEqual(b.build_memory_index(root)['manifest']['index_sha256'], expected)
            module.write_text(module.read_text() +
                              '\n# @anchor:wa.pkg.mod.caller\ndef caller():\n    return pub_fn()\n')
            with patch.object(b, '_walk', ast.walk):
                reference = b.build_memory_index(root)['files']
            self.assertEqual(b.build_memory_index(root)['files'], reference)
            # A traversal that drops descendants must fail byte equivalence.
            with patch.object(b, '_walk', side_effect=lambda tree: iter([tree])):
                self.assertNotEqual(b.build_memory_index(root)['files'], reference)

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_build_local_caches_and_single_source_read
    def test_build_local_caches_and_single_source_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = _fixture(root)
            reads = []
            original = Path.read_bytes

            def read_bytes(path):
                reads.append(path)
                return original(path)

            with patch.object(b, 'toks', wraps=b.toks) as tokens, \
                 patch.object(b, 'idf_ppm', wraps=b.idf_ppm) as idfs, \
                 patch.object(Path, 'read_bytes', read_bytes):
                first = b.build_memory_index(root)
            # Positive control: shared zone texts and DFs are computed once.
            texts = [call.args[0] for call in tokens.call_args_list]
            dfs = [call.args for call in idfs.call_args_list]
            self.assertEqual(len(texts), len(set(texts)))
            self.assertEqual(len(dfs), len(set(dfs)))
            self.assertEqual(sorted(reads), sorted(root / rel for rel in first['manifest']['sources']))
            # Must-FAIL fixture: edits with unchanged length/mtime still invalidate
            # an in-memory build; no process-global or stat-only cache is trusted.
            stamp = module.stat()
            module.write_text(module.read_text().replace('Search controls', 'Locate buttons'))
            os.utime(module, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            changed = b.build_memory_index(root)
            self.assertNotEqual(first['files'], changed['files'])
            self.assertEqual(changed['files'], b.build_memory_index(root)['files'])

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_stdlib_only_and_module_contract
    def test_stdlib_only_and_module_contract(self):
        anchors = []
        for name in ('build_code_index', 'route_code'):
            path = ROOT / 'tools' / (name + '.py')
            src = path.read_text()
            header = json.loads(src.splitlines()[0].split('@module ', 1)[1])
            self.assertEqual((header['layer'], header['owner'], header['book']), (0, 'ARCH', 'HB-WA-a0-arch'))
            for node in ast.walk(ast.parse(src)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                for mod in names:
                    self.assertIn(mod.split('.')[0], sys.stdlib_module_names | {'build_code_index'})
            anchors += [m.group(1) for line in src.splitlines() if (m := b.ANCHOR_RE.match(line))]
        self.assertEqual(len(anchors), len(set(anchors)))


    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_rb_check_fails_before_backfill_on_fixture_module
    def test_rb_check_fails_before_backfill_on_fixture_module(self):
        # Predicate: the check rejects an unanchored symbol, then accepts its repair.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture(root)
            module = root / 'wender/perceive/fixture_unanchored.py'
            module.parent.mkdir()
            header = '# @module {"id":"wa.perceive.fixture_unanchored","layer":0,"owner":"PERCEIVE","book":"HB-WA-s1-perceive"}\n'
            body = 'def unanchored_fn(x):\n    return x\n'
            module.write_text(header + body)
            # Must-FAIL fixture: CLI and index both expose the exact missing symbol.
            rc, report, _ = _invoke(r.main, ['--root', tmp, '--check'])
            self.assertEqual((rc, report['ok'], report['missing_count']), (4, False, 1))
            missing = b.build_memory_index(root)['missing_public']
            self.assertEqual([(m['file'], m['symbol']) for m in missing],
                             [('wender/perceive/fixture_unanchored.py', 'unanchored_fn')])
            module.write_text(header + '# @anchor:wa.perceive.fixture_unanchored.unanchored_fn\n' + body)
            # Positive control: the same fixture passes after comment-only backfill.
            rc, report, _ = _invoke(r.main, ['--root', tmp, '--check'])
            self.assertEqual((rc, report['ok'], report['missing']), (0, True, []))
            ix = b.build_memory_index(root)
            self.assertEqual(ix['missing_public'], [])
            row = next(d for d in ix['anchors'] if d['id'] == 'wa.perceive.fixture_unanchored.unanchored_fn')
            self.assertEqual(row['symbol'], 'unanchored_fn')

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_rb_cu_and_touched_modules_zero_missing_anchors
    def test_rb_cu_and_touched_modules_zero_missing_anchors(self):
        # Scope is the CU inventory in RB, excluding unrelated legacy modules.
        perceive = ('ax browser cdp_pipe cdp_port tab_manager session_registry parallel_perceive '
                    'vision_engine_real atspi atspi_tree disambig element_table harvest ladder '
                    'mark_node_join srf_identity stopstate text transform vision_adapter vision_bridge wait').split()
        learn = ('cu_tasks model_client calibrate cascade chrome_driver cu_loop cu_memory '
                 'dream_journal drift explore memory replay segmenter').split()
        scoped = {'wender/perceive/' + name + '.py' for name in perceive}
        scoped.update('wender/learn/' + name + '.py' for name in learn)
        scoped.update({'tools/cu_live.py', 'tools/cu_offline_e2e.py', 'tools/cu_web.py',
                       'tools/build_code_index.py', 'tools/route_code.py', 'wender/state/schema.py'})
        self.assertTrue(all((ROOT / rel).is_file() for rel in scoped))
        rc, report, _ = _invoke(r.main, ['--root', str(ROOT), '--check'])
        self.assertEqual([m for m in report['missing'] if m['file'] in scoped], [])
        self.assertLessEqual(report['missing_count'], 552)   # ratchet: new anchors may only lower the legacy gap
        # Negative control: whole-repo legacy failures must remain visible.
        self.assertEqual((rc, report['ok']), (4, False))
        self.assertTrue(any(m['file'] == 'tools/reseal.py' and m['symbol'] == 'dump'
                            for m in report['missing']))

    # @anchor:wa.tools.test_route_code_unit.RouteCodeTests.test_rb_backfilled_anchors_recorded_in_hyperbook_stages
    def test_rb_backfilled_anchors_recorded_in_hyperbook_stages(self):
        cases = {
            'HB-WA-a0-arch': ['wa.state.schema.StateError', 'wa.state.schema.canonical_actuation_resource'],
            'HB-WA-s1-perceive': [
                'wa.perceive.ax.sha256_text', 'wa.perceive.ax.release', 'wa.perceive.ax.canon',
                'wa.perceive.ax.context_matches', 'wa.perceive.ax.describe', 'wa.perceive.ax.level_allows_irreversible',
                'wa.perceive.browser.HandleError', 'wa.perceive.browser.StaleGenerationError',
                'wa.perceive.browser.EpochChangedError', 'wa.perceive.browser.SessionDisagreementError',
                'wa.perceive.browser.AmbiguousDiscoveryError', 'wa.perceive.browser.SharedSessionError',
                'wa.perceive.browser.PerceptionFocusError', 'wa.perceive.browser.inpage_script',
                'wa.perceive.cdp_pipe.CdpError', 'wa.perceive.cdp_pipe.WriterFence', 'wa.perceive.cdp_pipe.CdpPipe',
                'wa.perceive.cdp_pipe._rmtree_profile', 'wa.perceive.cdp_port.CdpTabError',
                'wa.perceive.tab_manager.TabManager', 'wa.perceive.session_registry.SessionRegistry',
                'wa.perceive.parallel_perceive.ObservationError',
                'wa.perceive.vision_engine_real.FrameValidationError', 'wa.perceive.vision_engine_real.InvalidDprError',
                'wa.perceive.vision_engine_real.InvalidFrameSequenceError', 'wa.perceive.vision_engine_real.InvalidTargetError',
                'wa.perceive.vision_engine_real.FrameDtypeError', 'wa.perceive.vision_engine_real.RunnerDeathError',
                'wa.perceive.vision_engine_real.validate_window_frames',
                'wa.perceive.disambig.disambiguate', 'wa.perceive.element_table.assemble',
                'wa.perceive.mark_node_join.join_marks_nodes', 'wa.perceive.srf_identity.srf_hash',
                'wa.perceive.transform.estimate_transform', 'wa.perceive.vision_bridge.SubprocessVisionEngine'],
            'HB-WA-s3-remember': ['wa.learn.cu_loop.run_loop', 'wa.learn.cu_memory.Receipt',
                                  'wa.learn.dream_journal.JournalWriter'],
            'HB-WA-s4-learn': ['wa.learn.cu_tasks.predicate_distance', 'wa.learn.cu_tasks.task_family_hint',
                               'wa.learn.cu_tasks.observation_status', 'wa.learn.model_client.evidence_value',
                               'wa.learn.model_client.bounded_output', 'wa.learn.calibrate.select_thresholds',
                               'wa.learn.cascade.CascadePolicy', 'wa.learn.drift.check_drift',
                               'wa.tools.cu_live.main', 'wa.tools.cu_offline_e2e.main', 'wa.tools.cu_web.main'],
        }
        for stage, anchors in cases.items():
            for anchor in anchors:
                with self.subTest(anchor=anchor):
                    env, rc = r.lookup_anchor(self.ix, anchor, budget=12000)
                    self.assertEqual((rc, env['abstain']['flag']), (0, False))
                    row = env['r'][0]
                    self.assertEqual(row['stage'], stage)
                    self.assertIn(stage + '#hb-s0', row['hb_sections'])
                    # Predicate catches the formerly misplaced CdpPipe anchor too.
                    self.assertEqual(row['symbol'], anchor.rsplit('.', 1)[1])
        # Must-FAIL control: a real anchor in the wrong stage is never admitted.
        env, rc = r.list_stage(self.ix, 'learn#hb-s0', k=50, budget=12000)
        self.assertEqual(rc, 0)
        self.assertNotIn('wa.perceive.cdp_pipe.CdpPipe', {row['id'] for row in env['r']})
        env, rc = r.lookup_anchor(self.ix, 'wa.perceive.cdp_pipe.MissingFixture')
        self.assertEqual((rc, env['abstain']['flag'], env['r']), (3, True, []))

if __name__ == '__main__':
    unittest.main()
