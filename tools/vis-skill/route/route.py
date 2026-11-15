#!/usr/bin/env python3
"""Routed map of this repository: where things are, what must hold, how they fail, and a compiled context pack per task.

Findings are model output pinned to a commit, with no human review unless route/manifest.json says so. Each finding is
marked against the working tree: FRESH (cited bytes unchanged), SHIFT (same bytes, moved), STALE (changed or gone),
UNKNOWN (no citation). They are coordinates, not proof: read the cited code before you state a cause.
Standard library only, offline, reads the tree. Only the index build writes, and only under route/index/: on the first
call, after HEAD moves, or on `build`. In a read-only tree the index is kept in memory instead.

  python3 route/route.py card                      L0: what this repository is, in about 1,500 tokens
  python3 route/route.py locate "<symptom>"        files to open, ranked (path, symbols, literals, findings, questions)
  python3 route/route.py ask "<question>"          findings that answer it, with state and cited lines
  python3 route/route.py pack "<task>" --budget N  L0 + findings + folder dossiers + cited code within N tokens (bytes/3)
  python3 route/route.py find <symbol> | show <path::symbol|path#La-b|path@s-e> | file <path> | map | check | stats
  python3 route/route.py tests <symbol|path> | history <path>   tests that name it; commits that touched it (fixes flagged)
  python3 route/route.py port <piece id>       newton-py: Go source, tests, Python module, port contract findings
  python3 route/route.py build                     rebuild route/index/ (symbols, file list) from the tree; seconds
"""
import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import warnings

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
INDEX = HERE / 'index'
STOP = frozenset('the a an of in on to is are was for and or with without when then that this it its by from at as be not no '
                 'never only every about get gets give after what why how where which does do did can could should would '
                 'there their into than so if else'.split())
TEST = re.compile(r'(^|/)(test|tests|testdata|fixtures?|goldens?)(/|$)|(^|/)test_[^/]*\.py$|_test\.(go|py)$')
CODE = frozenset({'.py', '.go', '.sh', '.js', '.ts', '.rs', '.c', '.h'})
DOCS = frozenset({'.md', '.txt', '.toml'})
LITERAL = re.compile(r'"((?:[^"\\\n]|\\.){6,200})"|\'((?:[^\'\\\n]|\\.){6,200})\'')
GO_DECL = re.compile(rb'^(?:func (?:\(\s*\w+\s+\*?(\w+)[^)]*\)\s*)?(\w+)|type (\w+))', re.M)
SH_FUNC = re.compile(rb'^(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{', re.M)
RRF = 60
RANKED = 40


def words(value):
    value = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', value)
    return [w.lower() for w in re.findall(r'[A-Za-z0-9]+', value)]


def terms(query):
    return sorted({w for w in words(query) if w not in STOP and len(w) > 1})


def digest(data):
    return hashlib.sha256(data).hexdigest()[:16]


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line] if path.exists() else []


def tokens(text):
    # bytes/3: at or above the o200k count on all 201 calibration packs (min 3.08, median 3.48 bytes per token)
    return (len(text.encode('utf-8')) + 2) // 3


def tree_head(root):
    head = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    return head.stdout.decode().strip()[:12] or None


def put(path, text):
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')          # concurrent builders and readers never see a partial file
    try:
        tmp.write_text(text, encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def is_python(root, name):
    if name.endswith('.py'):
        return True
    if '.' in name.rsplit('/', 1)[-1]:
        return False
    try:
        return re.match(rb'#![^\n]*python', (root / name).read_bytes()[:80]) is not None
    except OSError:
        return False


def tracked(root):
    done = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if done.returncode == 0 and done.stdout:
        return sorted(n for n in done.stdout.decode('utf-8', 'surrogateescape').split('\0') if n)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '.git' not in p.parts)


def line_starts(data):
    starts, pos = [0], data.find(b'\n')
    while pos != -1:
        starts.append(pos + 1)
        pos = data.find(b'\n', pos + 1)
    return starts


def py_symbols(name, data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            tree = ast.parse(data)
    except (SyntaxError, ValueError):
        return []
    starts, out = line_starts(data), []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = prefix + child.name
                first = min([child.lineno] + [d.lineno for d in child.decorator_list])
                end = child.end_lineno
                s, e = starts[first - 1], starts[end] if end < len(starts) else len(data)
                out.append([name, qual, 'class' if isinstance(child, ast.ClassDef) else 'function', s, e])
                walk(child, qual + '.')
    walk(tree, '')
    return out


def go_symbols(name, data):
    hits = list(GO_DECL.finditer(data))
    out = []
    for i, m in enumerate(hits):
        recv, fn, typ = (g.decode() if g else None for g in m.groups())
        qual = typ or (f'{recv}.{fn}' if recv else fn)
        end = hits[i + 1].start() if i + 1 < len(hits) else len(data)
        out.append([name, qual, 'type' if typ else 'function', m.start(), end])
    return out


def sh_symbols(name, data):
    out = []
    for m in SH_FUNC.finditer(data):
        close = data.find(b'\n}', m.end())
        out.append([name, m.group(1).decode(), 'function', m.start(), len(data) if close == -1 else close + 2])
    return out


def scan_tree(root):
    """File list and symbols with byte spans, in memory. Byte-stable for a given tree."""
    names = tracked(root)
    files, symbols = [], []
    for name in names:
        if name.startswith('route/'):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes().replace(b'\r\n', b'\n')
        suffix = os.path.splitext(name)[1]
        py = is_python(root, name)
        kind = 'code' if (suffix in CODE or py) else ('doc' if suffix in DOCS else 'other')
        files.append({'path': name, 'kind': kind, 'test': bool(TEST.search(name)), 'bytes': len(data), 'sha16': digest(data)})
        if py:
            symbols += py_symbols(name, data)
        elif suffix == '.go':
            symbols += go_symbols(name, data)
        elif suffix == '.sh' or (kind == 'other' and data[:2] == b'#!' and b'sh' in data[:40].split(b'\n')[0]):
            symbols += sh_symbols(name, data)
    return files, symbols


def build(root=ROOT, scanned=None):
    """Rebuild route/index/ from the tree."""
    files, symbols = scanned or scan_tree(root)
    INDEX.mkdir(exist_ok=True)
    put(INDEX / 'files.jsonl', ''.join(json.dumps(f, sort_keys=True) + '\n' for f in files))
    put(INDEX / 'symbols.jsonl', ''.join(json.dumps(s) + '\n' for s in symbols))
    meta = {'files': len(files), 'symbols': len(symbols), 'tree_head': tree_head(root)}
    put(INDEX / 'meta.json', json.dumps(meta, sort_keys=True, indent=1) + '\n')   # last: its head marks a complete index
    return meta


class Map:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        meta = json.loads((INDEX / 'meta.json').read_text(encoding='utf-8')) if (INDEX / 'meta.json').exists() else None
        rows = None
        if not meta or not (INDEX / 'files.jsonl').exists() or meta.get('tree_head') != tree_head(self.root):
            rows = scan_tree(self.root)                                 # missing, or built at another commit
            try:
                build(self.root, rows)
            except OSError:                                             # read-only tree or caged worker: keep the index in memory
                pass
        self.manifest = json.loads((HERE / 'manifest.json').read_text(encoding='utf-8')) if (HERE / 'manifest.json').exists() else {}
        files, symbols = rows or (jsonl(INDEX / 'files.jsonl'), jsonl(INDEX / 'symbols.jsonl'))
        self.files = files
        self.symbols = [{'path': r[0], 'name': r[1], 'kind': r[2], 'start': r[3], 'end': r[4]} for r in symbols]
        self.nodes = jsonl(HERE / 'findings.jsonl')
        self.cache = {}
        self._docs = None

    def read(self, path):
        if path not in self.cache:
            p = self.root / path
            self.cache[path] = p.read_bytes().replace(b'\r\n', b'\n') if p.is_file() else None
        return self.cache[path]

    @staticmethod
    def path_of(sid):
        return re.split(r'::|#L', sid, maxsplit=1)[0]

    def cite(self, cite):
        sid, sha, start, length = cite
        path = self.path_of(sid)
        data = self.read(path)
        if sha is None:
            return 'UNKNOWN', path, None, None
        if data is None:
            return 'STALE', path, None, None
        if sid == path:                                    # whole-file citation (dossiers): the file must be byte-identical
            return ('FRESH', path, 0, len(data)) if digest(data) == sha else ('STALE', path, None, None)
        if digest(data[start:start + length]) == sha:
            return 'FRESH', path, start, start + length
        hits = [o for o in line_starts(data) if o + length <= len(data) and digest(data[o:o + length]) == sha]
        if len(hits) == 1:
            return 'SHIFT', path, hits[0], hits[0] + length
        return 'STALE', path, None, None

    def lines(self, path, start, end):
        data = self.read(path)
        return data.count(b'\n', 0, start) + 1, data.count(b'\n', 0, max(start, end - 1)) + 1

    def row(self, node, score=None):
        states, code = [], []
        for c in node.get('s', []):
            st, path, s, e = self.cite(c)
            states.append(st)
            if s is not None and len(code) < 3:
                a, b = self.lines(path, s, e)
                code.append(f'{path}#L{a}-{b}')
            elif len(code) < 3:
                code.append(path)
        worst = next((s for s in ('STALE', 'UNKNOWN', 'SHIFT') if s in states), 'FRESH' if states else 'UNKNOWN')
        out = {'state': worst, 'level': node['l'], 'kind': node.get('k'), 'text': node['t'], 'code': code}
        if score is not None:
            out['score'] = round(score, 3)
        return out

    def docs(self):
        if self._docs is None:
            self._docs = [set(words(n['t'] + ' ' + ' '.join(n.get('q', [])))) | {w for c in n.get('s', []) for w in words(c[0].split('::')[-1])}
                          for n in self.nodes]
        return self._docs

    def rank_nodes(self, query, levels=None):
        wanted = terms(query)
        docs = self.docs()
        if not wanted or not docs:
            return []
        df = {w: sum(1 for d in docs if w in d) for w in wanted}
        idf = {w: math.log(1 + (len(docs) + 1) / (1 + df[w])) for w in wanted}
        scored = [(sum(idf[w] for w in wanted if w in d), i) for i, d in enumerate(docs)
                  if (levels is None or self.nodes[i]['l'] in levels)]
        return [(s, self.nodes[i]) for s, i in sorted(scored, key=lambda x: (-x[0], x[1])) if s > 0]

    def ask(self, query, k):
        out, seen, dossiers = [], set(), 0
        for s, n in self.rank_nodes(query, {'L2', 'PORT', 'L1', 'Q'}):
            if n['t'] in seen or (n['l'] == 'L1' and dossiers):            # one dossier at most: long texts match every query
                continue
            dossiers += n['l'] == 'L1'
            seen.add(n['t'])
            out.append(self.row(n, s))
            if len(out) == k:
                break
        return out

    def literals(self):
        rows = []
        for f in self.files:
            if f['kind'] != 'code' or f['test']:
                continue
            text = (self.read(f['path']) or b'').decode('utf-8', 'replace')
            for m in LITERAL.finditer(text):
                lit = m.group(1) or m.group(2)
                found = set(words(lit))
                if found:
                    rows.append((f['path'], lit, found, text.count('\n', 0, m.start()) + 1))
        return rows

    def error(self, query, k):
        wanted = terms(query)
        rows = self.literals()
        if not wanted or not rows:
            return []
        df = {w: sum(1 for r in rows if w in r[2]) for w in wanted}
        idf = {w: math.log(1 + (len(rows) + 1) / (1 + df[w])) for w in wanted}
        scored = sorted(((-sum(idf[w] for w in wanted if w in r[2]), r[0], r[3], r[1]) for r in rows), key=lambda x: x[:3])
        return [{'path': p, 'line': ln, 'text': t[:160]} for s, p, ln, t in scored[:k] if s < 0]

    def locate(self, query, k):
        wanted = set(terms(query))
        files = [f['path'] for f in self.files if f['kind'] in ('code', 'doc') and not f['test']]
        ranked = lambda scores: [p for p, v in sorted(scores.items(), key=lambda x: (-x[1], x[0])) if v > 0][:RANKED]
        sym = {}
        for s in self.symbols:
            v = len(set(words(s['name'])) & wanted)
            if v:
                sym[s['path']] = max(sym.get(s['path'], 0), v)
        lit = []
        for r in self.error(query, 200):
            if r['path'] not in lit:
                lit.append(r['path'])
        finding, question = [], []
        for _, n in self.rank_nodes(query)[:RANKED * 2]:
            target = question if n['l'] == 'Q' else finding
            for c in n.get('s', [])[:1]:
                p = self.path_of(c[0])
                if p not in target:
                    target.append(p)
        rankings = {'path': ranked({p: len(set(words(p)) & wanted) for p in files}), 'symbol': ranked(sym), 'literal': lit[:RANKED],
                    'finding': finding[:RANKED], 'question': question[:RANKED]}
        votes, via = {}, {}
        for source, ordered in rankings.items():
            for pos, p in enumerate(ordered, 1):
                votes[p] = votes.get(p, 0) + 1 / (RRF + pos)
                via.setdefault(p, []).append(source)
        return [{'path': p, 'via': via[p]} for p, _ in sorted(votes.items(), key=lambda x: (-x[1], x[0]))[:k]]

    def find(self, query, k):
        exact = [s for s in self.symbols if s['name'] == query or s['name'].rsplit('.', 1)[-1] == query]
        wanted = set(terms(query))
        rest = sorted((s for s in self.symbols if s not in exact and set(words(s['name'])) & wanted),
                      key=lambda s: -len(set(words(s['name'])) & wanted))
        out = []
        for s in (exact + rest)[:k]:
            a, b = self.lines(s['path'], s['start'], s['end'])
            out.append({'sid': f"{s['path']}::{s['name']}", 'kind': s['kind'], 'lines': f'{a}-{b}'})
        return out

    def show(self, query):
        m = re.match(r'^(.*)#L(\d+)-(\d+)$', query) or re.match(r'^(.*)@(\d+)-(\d+)$', query)
        if m and '#L' in query:
            path = m.group(1)
            data = self.read(path)
            if data is None:
                return {'path': path, 'code': None}
            st = line_starts(data)
            a, b = int(m.group(2)), int(m.group(3))
            return {'path': path, 'lines': f'{a}-{b}', 'code': data[st[a - 1]:st[b] if b < len(st) else len(data)].decode('utf-8', 'replace')}
        if m:
            path, s, e = m.group(1), int(m.group(2)), int(m.group(3))
        else:
            hit = next((x for x in self.symbols if f"{x['path']}::{x['name']}" == query), None)
            if not hit:
                return {'error': 'E_SHOW_UNKNOWN', 'query': query}
            path, s, e = hit['path'], hit['start'], hit['end']
        data = self.read(path)
        return {'path': path, 'code': None if data is None else data[s:e].decode('utf-8', 'replace')}

    def file(self, path, k):
        rows = [self.row(n) for n in self.nodes if n['l'] in ('L2', 'PORT') and any(self.path_of(c[0]) == path for c in n.get('s', []))]
        return {'path': path, 'symbols': [s['name'] for s in self.symbols if s['path'] == path][:k * 5],
                'findings': rows[:k * 5], 'finding_count': len(rows)}

    def folders(self):
        return [{'files': n.get('files'), 'summary': n['t'][:300]} for n in self.nodes if n['l'] == 'L1']

    def card(self):
        p = HERE / 'card.md'
        return p.read_text(encoding='utf-8') if p.exists() else 'E_NO_CARD: route/card.md not generated yet; use map and ask'

    def pack(self, task, budget):
        """Compiled context for one task: L0 card, ranked FRESH/SHIFT findings, relevant non-stale L1 dossiers, then cited code, within budget."""
        parts, used, seen_code = [], 0, set()
        prose = budget - budget * 35 // 100                              # the last 35 % is kept for cited code

        def add(text, cap=budget):
            nonlocal used
            t = tokens(text)
            if used + t > cap:
                return False
            parts.append(text)
            used += t
            return True
        add('# Context pack\nTask: ' + task + '\nFindings are model output pinned to a commit; states are checked now. Read cited code before acting.\n')
        add('## Repository card\n' + self.card()[:6000], prose)
        rows = [r for r in self.ask(task, 40) if r['state'] in ('FRESH', 'SHIFT') and r['level'] != 'L1']
        lines = [f"- [{r['kind']}] {r['text']} ({', '.join(r['code'])})" for r in rows[:25]]
        for cut in (12, 8, 4):                                          # findings first: they are the most specific prose
            if add('## Findings\n' + '\n'.join(lines[:cut]), prose):
                break
        l1 = [n for _, n in self.rank_nodes(task, {'L1'}) if self.row(n)['state'] != 'STALE'][:2]  # a dossier is stale once any file it covers changed
        for n in l1:
            add('## Folder dossier\n' + n['t'][:3200], prose)
        for r in rows:
            for ref in r['code']:
                if '#L' in ref and ref not in seen_code:
                    seen_code.add(ref)
                    code = self.show(ref).get('code') or ''
                    if len(code) > 3000:
                        code = code[:code.rfind('\n', 0, 3000) + 1 or 3000] + '...\n'   # whole lines only
                    if code:
                        code += '' if code.endswith('\n') else '\n'
                        add(f"## Code {ref}\n```\n{code}```")              # a block that does not fit is skipped, not the end
        loc = self.locate(task, 8)
        add('## Files to open\n' + '\n'.join(f"- {x['path']} ({', '.join(x['via'])})" for x in loc))
        return {'tokens': used, 'budget': budget, 'pack': '\n\n'.join(parts)}

    def tests(self, query, k):
        """Test files that name the symbol or path (lexical), with the first line that does."""
        name = query.rsplit('::', 1)[-1].rsplit('.', 1)[-1] if '::' in query else query
        out = []
        for f in self.files:
            if f['test'] and len(name) >= 3:
                text = (self.read(f['path']) or b'').decode('utf-8', 'replace')
                i = text.find(name)
                if i != -1:
                    out.append({'path': f['path'], 'line': text.count('\n', 0, i) + 1})
        return out[:k]

    def history(self, query, k):
        """Commits that touched a path, newest first; subjects that look like fixes are flagged. Needs git."""
        done = subprocess.run(['git', '-C', str(self.root), 'log', f'-{max(k, 1)}', '--format=%h%x09%ad%x09%s', '--date=short', '--', query],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        rows = []
        for line in done.stdout.decode('utf-8', 'replace').splitlines():
            h, d, subj = (line.split('\t', 2) + ['', ''])[:3]
            rows.append({'commit': h, 'date': d, 'subject': subj, 'fix': bool(re.search(r'(?i)\b(fix|bug|regress|revert|broken|crash|wrong)', subj))})
        return rows

    def port(self, query, k):
        """newton-py only: piece -> go_ref source, tests, Python module and whether it exists, plus the PORT contract findings."""
        rows = [r for r in jsonl(HERE / 'port_map.jsonl') if query in (r['piece'], r['go_ref'], r.get('python_module'))]
        for r in rows[:k]:
            r['contract'] = [self.row(n) for n in self.nodes if n['l'] == 'PORT' and any(self.path_of(c[0]) == r['go_ref'] for c in n.get('s', []))][:25]
        return rows[:k]

    def check(self):
        states, dossiers = {}, {}
        for n in self.nodes:
            box = dossiers if n['l'] == 'L1' else states
            s = self.row(n)['state']
            box[s] = box.get(s, 0) + 1
        total = sum(states.values()) or 1
        return {'states': states, 'fresh_share': round(states.get('FRESH', 0) / total, 3), 'dossiers': dossiers,
                'study_pin': self.manifest.get('study_pin'), 'advisory': True}

    def stats(self):
        levels = {}
        for n in self.nodes:
            levels[n['l']] = levels.get(n['l'], 0) + 1
        return {'files': len(self.files), 'symbols': len(self.symbols), 'nodes': levels} | {k: self.manifest.get(k) for k in ('study_pin', 'seats', 'review')}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['build', 'card', 'locate', 'ask', 'pack', 'find', 'show', 'file', 'map', 'error', 'tests', 'history', 'port', 'check', 'stats'])
    ap.add_argument('query', nargs='?', default='')
    ap.add_argument('--k', type=int, default=8)
    ap.add_argument('--budget', type=int, default=6000)
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if a.command == 'build':
        print(json.dumps(build(), sort_keys=True))
        return 0
    m = Map()
    if a.command == 'card':
        print(m.card())
        return 0
    if a.command == 'pack':
        out = m.pack(a.query, a.budget)
        print(out['pack'])
        print(f"\n<!-- route pack: {out['tokens']} of {out['budget']} tokens (bytes/3) -->")
        return 0
    out = {'show': lambda: m.show(a.query), 'file': lambda: m.file(a.query, a.k), 'map': lambda: {'rows': m.folders()},
           'check': m.check, 'stats': m.stats}.get(a.command, lambda: {'rows': getattr(m, a.command)(a.query, a.k)})()
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
