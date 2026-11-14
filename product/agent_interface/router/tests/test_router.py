#!/usr/bin/env python3
"""Tests for vp docs (router). Standard library only; run: python3 -I product/agent_interface/router/tests/test_router.py

Positive: 10 operator questions, each with the citation that must come first.
Negative controls: absent topic -> AUSENTE; edited cited bytes -> STALE; moved bytes -> SHIFT; bad seed -> AUDIT_VIOLATIONS;
missing engine -> DEPENDENCY; bad usage -> USAGE; dry-run writes nothing; committed findings match a rebuild.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROUTER = Path(__file__).resolve().parents[1]
AGENT_IF = ROUTER.parent
SPOOL = tempfile.mkdtemp(prefix='vpops-docs-spool-')

# (question, suffix of the file that must be cited first, literal that the cited lines must contain)
QUESTIONS = json.loads((Path(__file__).with_name('questions.json')).read_text(encoding='utf-8'))


def run(args, router=ROUTER, env=None):
    full = dict(os.environ, VP_SPOOL_DIR=SPOOL, **(env or {}))
    done = subprocess.run([sys.executable, '-I', str(router / 'vp_docs.py')] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=full, timeout=120, check=False)
    out = done.stdout.decode('utf-8', 'replace')
    lines = out.rstrip('\n').split('\n')
    try:
        rec = json.loads(lines[-1])
    except ValueError:
        rec = None
    return done.returncode, out, rec


def cited_text(cite):
    """Bytes of path:a-b as cited in a receipt."""
    path, _, span = cite.rpartition(':')
    a, _, b = span.partition('-')
    full = Path(path) if os.path.isabs(path) else AGENT_IF.parents[1] / path
    lines = full.read_text(encoding='utf-8', errors='replace').split('\n')
    return '\n'.join(lines[int(a) - 1:int(b or a)])


class Receipts(unittest.TestCase):
    def check_receipt(self, rc, rec):
        self.assertIsNotNone(rec, 'last stdout line must be the JSON receipt')
        self.assertEqual(sorted(rec), ['code', 'data', 'next', 'ok', 'rc'])
        self.assertEqual(rec['rc'], rc)
        self.assertEqual(rec['ok'], rc == 0)
        self.assertIsInstance(rec['next'], list)

    def test_every_command_ends_with_one_receipt_and_is_bounded(self):
        for args in (['card'], ['ask', 'stale frame'], ['pack', 'record a capsule', '--budget', '2500'], ['locate', 'receipt'],
                     ['check'], ['stats'], ['file', 'product/agent_interface/docs/TROUBLESHOOTING.md'], ['find', 'receipt'],
                     ['show', 'product/agent_interface/docs/AGENT_GUIDE.md#L1-5']):
            rc, out, rec = run(args)
            self.check_receipt(rc, rec)
            self.assertEqual(rc, 0, f'{args}: {out[-300:]}')
            self.assertLessEqual(len(out.rsplit('\n', 2)[0]), 4096, args)             # human text bound; the receipt is the last line
            self.assertEqual(sum(1 for l in out.split('\n') if l.startswith('{"code"')), 1, args)

    def test_usage_is_typed(self):
        for args in (['ask'], ['nonsense'], ['pack', '']):
            rc, _, rec = run(args)
            self.assertEqual((rc, rec['code']), (2, 'USAGE'), args)

    def test_dry_run_writes_nothing(self):
        before = hashlib.sha256((ROUTER / 'findings.jsonl').read_bytes()).hexdigest()
        spooled = set(os.listdir(SPOOL))
        for args in (['build', '--dry-run'], ['pack', 'record a capsule', '--dry-run'], ['ask', 'x', '--dry-run']):
            rc, _, rec = run(args)
            self.assertEqual(rc, 0)
            self.assertEqual(rec['code'], 'DRY_RUN')
            self.assertIn('vp_docs.py', ' '.join(rec['data']['argv']))
        self.assertEqual(before, hashlib.sha256((ROUTER / 'findings.jsonl').read_bytes()).hexdigest())
        self.assertEqual(spooled, set(os.listdir(SPOOL)))

    def test_engine_missing_is_typed(self):
        rc, _, rec = run(['ask', 'stale frame'], env={'VP_ROUTE_ENGINE': '/nonexistent/route.py'})
        self.assertEqual((rc, rec['code']), (8, 'DEPENDENCY'))
        self.assertEqual(rec['data']['missing'], ['/nonexistent/route.py'])


class Questions(unittest.TestCase):
    def test_ten_questions_cite_the_expected_place_first(self):
        self.assertGreaterEqual(len(QUESTIONS), 10)
        for q in QUESTIONS:
            rc, out, rec = run(['ask', q['q'], '--k', '3'])
            self.assertEqual(rc, 0, f"{q['q']}: {out[-400:]}")
            top = rec['data']['rows'][0]
            self.assertIn(top['mark'], ('FRESH', 'SHIFT'), q['q'])
            cite = top['cite']
            self.assertTrue(cite.rpartition(':')[0].endswith(q['file']), f"{q['q']}: top citation {cite}, expected {q['file']}")
            self.assertIn(q['literal'], cited_text(cite), f"{q['q']}: {cite} does not hold {q['literal']!r}")
            self.assertRegex(out, r'1\. \[(FRESH|SHIFT)\] \S+:\d+')

    def test_absent_topics_return_ausente(self):
        for q in ('kubernetes helm chart rollout', 'how do I pay the invoice with a credit card', 'configure the Postgres replica'):
            rc, out, rec = run(['ask', q])
            self.assertEqual((rc, rec['code'], rec['ok']), (5, 'AUSENTE', False), q)
            self.assertIn('AUSENTE', out)
            self.assertNotIn('rows', rec['data'])
            self.assertTrue(rec['data']['absent_terms'], q)
        rc, _, rec = run(['show', 'product/agent_interface/docs/NO_SUCH_PAGE.md#L1-2'])
        self.assertEqual((rc, rec['code']), (5, 'NOT_FOUND'))
        rc, _, rec = run(['route', 'kubernetes helm chart rollout'])
        self.assertEqual((rc, rec['code'], rec['data']['commands']), (5, 'AUSENTE', []))

    def test_route_names_the_command_with_a_citation(self):
        for q, argv in (('record a display to mp4', ['vp', 'record', 'start']), ('enqueue an intent for an agent', ['vp', 'queue', 'enqueue']),
                        ('restore a checkpoint', ['vp', 'rewind', 'restore']), ('hand control to the operator', ['vp', 'human', 'handback'])):
            rc, out, rec = run(['route', q])
            self.assertEqual(rc, 0, out[-300:])
            self.assertIn(argv, [c['argv'] for c in rec['data']['commands']], q)
            self.assertRegex(rec['data']['commands'][0]['cite'], r'COMMANDS\.md:\d+')
            self.assertEqual(rec['data']['commands'][0]['mark'], 'FRESH')

    def test_pack_is_byte_stable_and_within_budget(self):
        a = run(['pack', 'recover from STALE_FRAME during vision act', '--budget', '3000'])
        b = run(['pack', 'recover from STALE_FRAME during vision act', '--budget', '3000'])
        self.assertEqual(a[0], 0)
        self.assertEqual(a[2]['data']['spool']['sha256'], b[2]['data']['spool']['sha256'])
        self.assertLessEqual(a[2]['data']['tokens'], 3000)
        text = Path(a[2]['data']['spool']['path']).read_text(encoding='utf-8')
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), a[2]['data']['spool']['sha256'])
        self.assertNotRegex(text, r'\b\d{2}:\d{2}:\d{2}\b|\bpid \d+')                 # no clock values or PIDs in a pack
        self.assertLessEqual(len(text.encode()) // 3, 3000 + 1)
        rc, _, rec = run(['pack', 'kubernetes helm chart rollout', '--budget', '1500'])
        self.assertEqual((rc, rec['code']), (5, 'AUSENTE'))


class Fixture(unittest.TestCase):
    """A private copy of the route, so citations can be broken on purpose."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='vpops-docs-fixture-'))
        self.router = self.tmp / 'product' / 'agent_interface' / 'router'
        self.docs = self.tmp / 'product' / 'agent_interface' / 'docs'
        self.router.mkdir(parents=True)
        self.docs.mkdir()
        shutil.copy(ROUTER / 'vp_docs.py', self.router)
        (self.router / 'card.md').write_text('# fixture card\n')
        (self.router / 'sources.json').write_text(json.dumps({'docs': [['product/agent_interface/docs/*.md']], 'code': []}))
        (self.router / 'seeds.jsonl').write_text('')
        (self.docs / 'PAGE.md').write_text('# Page\n\n## Zebra\n\nThe zebra crossing verb needs the quokka flag before launch.\n\n'
                                           '## Other\n\nAn unrelated paragraph about walnut storage and drying.\n')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def ask(self, q='zebra quokka flag'):
        rc, out, rec = run(['ask', q, '--k', '1'], router=self.router)
        return rc, rec

    def test_fresh_then_shift_then_stale(self):
        self.assertEqual(run(['build', '--strict'], router=self.router)[0], 0)
        rc, rec = self.ask()
        self.assertEqual((rc, rec['data']['rows'][0]['mark'], rec['data']['rows'][0]['cite']), (0, 'FRESH', 'product/agent_interface/docs/PAGE.md:5'))
        page = self.docs / 'PAGE.md'
        page.write_text(page.read_text().replace('# Page\n', '# Page\n\nA new first paragraph, two lines up.\n'))
        rc, rec = self.ask()
        self.assertEqual((rec['data']['rows'][0]['mark'], rec['data']['rows'][0]['cite']), ('SHIFT', 'product/agent_interface/docs/PAGE.md:7'))
        page.write_text(page.read_text().replace('quokka flag', 'wombat flag'))
        rc, rec = self.ask()
        self.assertEqual(rec['data']['rows'][0]['mark'], 'STALE')
        self.assertIn('vp docs build', rec['next'])
        rc, _, chk = run(['check'], router=self.router)
        self.assertEqual((rc, chk['code'], chk['data']['stale']), (1, 'AUDIT_VIOLATIONS', 1))
        self.assertEqual(run(['build'], router=self.router)[0], 0)             # rebuild heals it
        rc, rec = self.ask('zebra wombat flag')
        self.assertEqual(rec['data']['rows'][0]['mark'], 'FRESH')

    def test_seed_pins_to_anchor_line_and_bad_seed_fails_strict(self):
        code = self.tmp / 'tool.sh'
        code.write_text('#!/bin/sh\n# header\nexit 7  # MARMOT_LOCKED\n')
        good = {'family': 'x', 'kind': 'failure', 'text': 'Exit 7 means MARMOT_LOCKED: release the marmot lock.', 'path': str(code), 'anchor': 'MARMOT_LOCKED'}
        (self.router / 'seeds.jsonl').write_text(json.dumps(good) + '\n')
        self.assertEqual(run(['build', '--strict'], router=self.router)[0], 0)
        rc, rec = self.ask('marmot lock exit 7')
        self.assertEqual(rec['data']['rows'][0]['cite'], 'tool.sh:3')                 # inside the fixture repository, so repo-relative
        for bad in ({**good, 'anchor': 'NO_SUCH_LITERAL'}, {**good, 'anchor': 'e'}, {**good, 'path': str(self.tmp / 'gone.sh')}):
            (self.router / 'seeds.jsonl').write_text(json.dumps(bad) + '\n')
            rc, _, rec = run(['build', '--strict'], router=self.router)
            self.assertEqual((rc, rec['code'], rec['data']['bad_seeds']), (1, 'AUDIT_VIOLATIONS', 1), bad)
            rc, rec = self.ask('marmot lock exit 7')
            self.assertEqual(rec['code'], 'AUSENTE')                           # an unpinned seed is never served

    def test_absent_in_fixture(self):
        self.assertEqual(run(['build'], router=self.router)[0], 0)
        rc, rec = self.ask('giraffe telemetry')
        self.assertEqual((rc, rec['code']), (5, 'AUSENTE'))


class Committed(unittest.TestCase):
    def test_committed_findings_match_a_rebuild_of_repo_docs(self):
        """Every finding that cites a file of this repository must be FRESH: the docs were not edited after the last build."""
        import importlib.util
        spec = importlib.util.spec_from_file_location('vp_docs', ROUTER / 'vp_docs.py')
        vp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vp)
        engine, _ = vp.load_engine()
        vp.install(engine)
        m = engine.Map(vp.REPO)
        local = [n for n in m.nodes if not os.path.isabs(n['s'][0][0])]
        self.assertGreater(len(local), 100)
        stale = [n['s'][0][0] for n in local if m.row(n)['state'] != 'FRESH']
        self.assertEqual(stale, [], 'run: vp docs build')
        self.assertEqual(m.manifest.get('bad_seeds'), [])


class FixOps(unittest.TestCase):
    def test_no_host_paths_in_router_code(self):
        # xr-ops-docs A1 (code part): the public gate finds no host path in the router or page-builder code.
        root = AGENT_IF.parents[1]
        gate = root / 'tools' / 'check_public.sh'
        if not gate.is_file():
            self.skipTest('tools/check_public.sh absent')
        out = subprocess.run([str(gate), '--no-history'], cwd=str(root), stdout=subprocess.PIPE, check=False).stdout.decode()
        bad = [l for l in out.splitlines() if '/router/vp_docs.py:' in l or '/docs/build_pages.py:' in l]
        self.assertEqual(bad, [])

    def test_guide_commands_are_router_commands(self):
        # xr-ops-docs A3: every `vp docs <verb>` of GUIDE.md is a command of vp_docs.py.
        import re
        src = (ROUTER / 'vp_docs.py').read_text(encoding='utf-8')
        choices = set(re.findall(r"'([a-z]+)'", re.search(r"add_argument\('command', choices=\[(.*?)\]", src).group(1)))
        verbs = set(re.findall(r'`vp docs ([a-z]+)', (ROUTER / 'GUIDE.md').read_text(encoding='utf-8')))
        self.assertTrue({'file', 'find', 'stats'} <= verbs)
        self.assertEqual(sorted(verbs - choices), [])

if __name__ == '__main__':
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(SPOOL, True)
