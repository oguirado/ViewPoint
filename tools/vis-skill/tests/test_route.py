"""Route map checks: offline, standard library, a few seconds. Run: python3 -I -m unittest discover -s tests -p 'test_route*.py'"""
import importlib.util
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / 'route'
BLOCKED = re.compile('/' + 'home/|/' + 'tmp/cl' + 'aude|/' + 'Users/|Documentos/|M[uú]sica/|[A-Za-z0-9._+-]+@[A-Za-z0-9-]+[.][a-z]{2,}')


def load(root):
    spec = importlib.util.spec_from_file_location('route_under_test', root / 'route' / 'route.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / 'repo'
        names = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], stdout=subprocess.PIPE, check=True).stdout.decode().split('\0')
        for n in [n for n in names if n] + ['route/route.py', 'route/findings.jsonl', 'route/manifest.json']:
            src = ROOT / n
            if src.is_file() and not src.is_symlink():
                dst = cls.root / n
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
        cls.mod = load(cls.root)
        cls.mod.INDEX = cls.root / 'route' / 'index'
        cls.mod.HERE = cls.root / 'route'

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_build_is_byte_stable(self):
        self.mod.build(self.root)
        first = [(p.name, p.read_bytes()) for p in sorted((self.root / 'route' / 'index').iterdir())]
        self.mod.build(self.root)
        second = [(p.name, p.read_bytes()) for p in sorted((self.root / 'route' / 'index').iterdir())]
        self.assertEqual(first, second)

    def test_findings_parse_and_cite_tracked_files(self):
        m = self.mod.Map(self.root)
        files = {f['path'] for f in m.files}
        self.assertTrue(m.nodes)
        for n in m.nodes:
            self.assertIn(n['l'], {'L1', 'L2', 'PORT', 'Q'})
            for c in n['s']:
                self.assertEqual(len(c), 4)
                self.assertIn(m.path_of(c[0]), files, c[0])

    def test_no_host_paths_or_emails_in_route_data(self):
        for name in ('findings.jsonl', 'manifest.json', 'card.md', 'GUIDE.md'):
            p = ROUTE / name
            if p.exists():
                self.assertIsNone(BLOCKED.search(p.read_text(encoding='utf-8')), name)

    def test_card_and_guide_cite_symbols_not_line_numbers(self):
        for name in ('card.md', 'GUIDE.md'):                                # an unhashed path:line in prose drifts silently
            p = ROUTE / name
            if p.exists():
                self.assertEqual(re.findall(r'[A-Za-z_][\w/.-]*:\d+', p.read_text(encoding='utf-8')), [], name)

    def test_queries_return_rows(self):
        m = self.mod.Map(self.root)
        q = m.nodes[0]['t']
        self.assertTrue(m.ask(q, 3))
        self.assertTrue(m.locate(q, 3))
        out = m.pack(q, 3000)
        self.assertLessEqual(out['tokens'], 3000)

    def test_changed_cited_bytes_turn_stale(self):
        m = self.mod.Map(self.root)
        cite = next(c for n in m.nodes if n['l'] != 'L1' for c in n['s'] if c[1] and m.cite(c)[0] == 'FRESH')
        path = self.root / m.path_of(cite[0])
        data = bytearray(path.read_bytes())
        i = cite[2] + cite[3] // 2
        data[i] = ord('#') if data[i] != ord('#') else ord('%')
        path.write_bytes(bytes(data))
        try:
            self.assertEqual(self.mod.Map(self.root).cite(cite)[0], 'STALE')   # must-fail leg: a changed byte is never FRESH
        finally:
            data[i] = (ROOT / m.path_of(cite[0])).read_bytes()[i]
            path.write_bytes(bytes(data))

    def test_changed_file_turns_dossier_stale(self):
        m = self.mod.Map(self.root)
        node = next((n for n in m.nodes if n['l'] == 'L1' and m.row(n)['state'] == 'FRESH'), None)
        if node is None:
            self.skipTest('no fresh dossier with hashed files')
        cite = next(c for c in node['s'] if c[1])
        path = self.root / m.path_of(cite[0])
        data = path.read_bytes()
        path.write_bytes(data + b'\n#')
        try:
            self.assertEqual(self.mod.Map(self.root).row(node)['state'], 'STALE')   # must-fail leg: a dossier over a changed file is stale
        finally:
            path.write_bytes(data)

    def test_index_rebuilds_when_head_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'route').mkdir()
            shutil.copyfile(self.root / 'route' / 'route.py', root / 'route' / 'route.py')
            (root / 'a.py').write_text('def first():\n    return 1\n', encoding='utf-8')
            git = lambda *a: subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@t', *a], stdout=subprocess.DEVNULL, check=True)
            git('init', '-q')
            git('add', '-A')
            git('commit', '-qm', 'one')
            mod = load(root)
            self.assertNotIn('b.py', {f['path'] for f in mod.Map(root).files})
            (root / 'b.py').write_text('def second():\n    return 2\n', encoding='utf-8')
            git('add', '-A')
            git('commit', '-qm', 'two')
            m = mod.Map(root)                                             # must-fail leg: an index from the old head is not reused
            self.assertIn('b.py', {f['path'] for f in m.files})
            self.assertEqual(json.loads((root / 'route' / 'index' / 'meta.json').read_text())['tree_head'], mod.tree_head(root))

    def test_read_verbs_work_in_a_read_only_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'ro'
            shutil.copytree(self.root / 'route', root / 'route', ignore=shutil.ignore_patterns('index'))   # route/ alone keeps it fast
            for p in [root, *root.rglob('*')]:
                p.chmod(p.stat().st_mode & 0o555)                         # a-w, as a read-only copy or a caged worker sees it
            try:
                probe = root / '.probe'
                try:
                    probe.write_text('x')                                       # root or a user namespace ignores a-w
                    probe.unlink()
                    self.skipTest('file permissions are not enforced here; the in-process leg covers it')
                except PermissionError:
                    pass
                for argv in (['card'], ['ask', self.mod.Map(self.root).nodes[0]['t'][:60], '--k', '2'], ['pack', 'route map', '--budget', '2000']):
                    p = subprocess.run([sys.executable, '-I', str(root / 'route' / 'route.py'), *argv], cwd=root,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
                    self.assertEqual(p.returncode, 0, argv[0] + p.stderr[-300:])   # must-fail leg: no PermissionError on a read verb
                self.assertFalse((root / 'route' / 'index').exists())
            finally:
                for p in [root, *root.rglob('*')]:
                    p.chmod(p.stat().st_mode | 0o200)

    def test_read_verbs_survive_an_unwritable_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(self.root / 'route', root / 'route', ignore=shutil.ignore_patterns('index'))
            mod = load(root)

            def refuse(*a):
                raise PermissionError('read-only')
            mod.put = refuse
            m = mod.Map(root)                                               # must-fail leg: the old code raised here
            self.assertTrue(m.ask(m.nodes[0]['t'][:60], 2))
            self.assertFalse((root / 'route' / 'index' / 'meta.json').exists())

    def test_index_write_never_leaves_a_partial_file(self):
        index = self.root / 'route' / 'index'
        self.mod.build(self.root)
        before = {p.name: p.read_bytes() for p in index.iterdir()}
        real = self.mod.os.replace

        def fail(*a):
            raise OSError('disk full')
        self.mod.os.replace = fail
        try:
            with self.assertRaises(OSError):
                self.mod.build(self.root)
        finally:
            self.mod.os.replace = real
        self.assertEqual({p.name: p.read_bytes() for p in index.iterdir()}, before)   # must-fail leg: a failed build keeps the old files whole

    def test_guide_examples_run(self):
        guide = ROUTE / 'GUIDE.md'
        if not guide.exists():
            self.skipTest('no route/GUIDE.md')
        blocks = re.findall(r'```sh\n(.*?)```', guide.read_text(encoding='utf-8'), re.S)
        cmds = [line for b in blocks for line in b.splitlines() if line.startswith('python3 -I route/route.py ')]
        self.assertGreaterEqual(len(cmds), 5)
        for cmd in cmds:                                                  # must-fail leg: a stale example breaks this test
            argv = shlex.split(cmd)[3:]
            p = subprocess.run([sys.executable, '-I', str(self.root / 'route' / 'route.py'), *argv], cwd=self.root,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, cmd + p.stderr[-300:])
            self.assertTrue(p.stdout.strip(), cmd)
            if argv[0] not in ('card', 'pack'):
                json.loads(p.stdout)


if __name__ == '__main__':
    unittest.main()
