#!/usr/bin/env python3
"""vp docs: cited documentation + command router for ViewPoint operators (any AI).

  vp docs card                      L0 card: what ViewPoint is, safety rules, first commands (~1,500 tokens)
  vp docs ask "<question>" [--k N]  findings that answer it; each cites file:line with FRESH/SHIFT/STALE
  vp docs pack "<task>" [--budget N] byte-stable Markdown brief (card + findings + cited code) within N tokens
  vp docs locate "<symptom>"        files to open, ranked
  vp docs route "<intent>"          the vp command(s) that do it, cited, plus the team skill from `newton route`
  vp docs show <path#La-b>          the cited lines (bounded)
  vp docs file <path>               findings and symbols of one file
  vp docs check | stats             citation states for the whole route; counts
  vp docs build [--strict]          regenerate findings.jsonl from docs + seeds.jsonl (run after editing docs)

Reuse, not copy: the ranking, the byte-pinned citation states, `pack`, `locate` and `show` are newton-py's route
engine (newton-py/route/route.py, class Map), imported at run time. This file adds only: the ViewPoint corpus
(sources.json, seeds.jsonl), the findings builder, the AUSENTE abstention rule and the vp receipt line.

Every call prints bounded human text and ends with ONE JSON receipt line {ok, code, rc, data, next}.
Codes follow design/COMMANDS.md section 4 (vpops/1.1): OK 0 | DRY_RUN 0 | AUDIT_VIOLATIONS 1 (check found STALE,
build --strict could not pin a seed) | USAGE 2 | AUSENTE 5 | NOT_FOUND 5 (show) | DEPENDENCY 8 (route engine missing).
Standard library only, offline. Writes only router/index/ (cache), the spool directory, and, on `build`,
router/findings.jsonl + router/manifest.json.
"""
import argparse
import glob
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
# No absolute host path in code: the workspace root holds the sibling checkouts (override: $VP_OPS_ROOT).
WORKSPACE = Path(os.environ.get('VP_OPS_ROOT') or REPO.parent)
ENGINE_DEFAULT = next((str(p) for p in (WORKSPACE / 'newton-py' / 'route' / 'route.py',
                                        Path.home() / 'Documentos' / 'newton-py' / 'route' / 'route.py') if p.is_file()),
                      str(WORKSPACE / 'newton-py' / 'route' / 'route.py'))
NPY = str(Path.home() / '.local' / 'share' / 'newton-py' / 'venv' / 'bin' / 'newton')
RC = {'OK': 0, 'DRY_RUN': 0, 'AUDIT_VIOLATIONS': 1, 'USAGE': 2, 'AUSENTE': 5, 'NOT_FOUND': 5, 'DEPENDENCY': 8}
COVERAGE_FLOOR = 0.5          # idf-weighted share of the question's terms the best finding must cover
TEXT_CAP = 420                # bytes of finding text kept per doc block
PRINT_CAP = 4096              # characters of human text per call (COMMANDS.md section 1); the rest is spooled or paged


def receipt(code, data=None, nxt=None):
    line = {'ok': RC[code] == 0, 'code': code, 'rc': RC[code], 'data': data or {}, 'next': nxt or []}
    print(json.dumps(line, ensure_ascii=False, sort_keys=True))
    return RC[code]


def load_engine():
    path = Path(os.environ.get('VP_ROUTE_ENGINE', ENGINE_DEFAULT))
    if not path.is_file():
        return None, str(path)
    spec = importlib.util.spec_from_file_location('newton_route_engine', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, str(path)


def sources():
    return json.loads((HERE / 'sources.json').read_text(encoding='utf-8'))


def expand(alternatives):
    """First alternative pattern with at least one existing file wins. Patterns are repo-relative or absolute."""
    for pat in alternatives if isinstance(alternatives, list) else [alternatives]:
        full = pat if os.path.isabs(pat) else str(REPO / pat)
        hits = sorted(p for p in glob.glob(full, recursive=True) if os.path.isfile(p))
        if hits:
            return hits
    return []


def rel(path):
    """Repo-relative when inside this repository, absolute otherwise: the form used in citations."""
    p = os.path.abspath(path)
    root = str(REPO) + os.sep
    return p[len(root):] if p.startswith(root) else p


def seeds():
    path = HERE / 'seeds.jsonl'
    rows = []
    if path.exists():
        for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if line.strip():
                row = json.loads(line)
                row['_line'] = n
                rows.append(row)
    return rows


def corpus_files():
    """(citation path, kind) for every file of the corpus: doc pages, key code, and every file a seed cites."""
    src, out = sources(), {}
    for alt in src.get('docs', []):
        for p in expand(alt):
            out[rel(p)] = 'doc'
    for alt in src.get('code', []):
        for p in expand(alt):
            out.setdefault(rel(p), 'code')
    for s in seeds():
        p = s.get('path', '')
        if p and (REPO / p).is_file():
            out.setdefault(rel(REPO / p), 'doc' if p.endswith(('.md', '.txt')) else 'code')
    return sorted(out.items())


def install(engine):
    """Point the imported engine at this route directory and at the ViewPoint corpus instead of `git ls-files`."""
    engine.HERE, engine.ROOT, engine.INDEX = HERE, REPO, HERE / 'index'

    def scan_tree(root):
        files, symbols = [], []
        for name, kind in corpus_files():
            path = Path(root) / name
            data = path.read_bytes().replace(b'\r\n', b'\n')
            files.append({'path': name, 'kind': kind, 'test': bool(engine.TEST.search(name)), 'bytes': len(data),
                          'sha16': engine.digest(data)})
            if kind == 'code':
                if engine.is_python(Path(root), name):
                    symbols += engine.py_symbols(name, data)
                elif name.endswith('.sh') or data[:2] == b'#!':
                    symbols += engine.sh_symbols(name, data)
        return files, symbols

    def tree_head(root):
        h = hashlib.sha256()
        for name, kind in corpus_files():
            st = os.stat(Path(root) / name)
            h.update(f'{name}\0{kind}\0{st.st_size}\0{st.st_mtime_ns}\n'.encode())
        return h.hexdigest()[:12]

    engine.scan_tree, engine.tree_head = scan_tree, tree_head
    return engine


# ---------------------------------------------------------------- findings builder

HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
ITEM = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+')
TABLE_RULE = re.compile(r'^\s*\|?\s*:?-{2,}')


def md_blocks(text):
    """Yield (first_line, last_line, heading_path, block_text) for paragraphs, list items, table rows and code fences."""
    lines = text.split('\n')
    stack, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        m = HEADING.match(line)
        if m:
            level = len(m.group(1))
            stack = [h for h in stack if h[0] < level] + [(level, m.group(2))]
            i += 1
            continue
        if not line.strip():
            i += 1
            continue
        head = ' > '.join(h[1] for h in stack)
        if line.lstrip().startswith('```'):
            j = i + 1
            while j < n and not lines[j].lstrip().startswith('```'):
                j += 1
            yield i + 1, min(j + 1, n), head, ' '.join(l.strip() for l in lines[i + 1:j])
            i = j + 1
            continue
        if line.lstrip().startswith('|'):
            if not TABLE_RULE.match(line) and not (i + 1 < n and TABLE_RULE.match(lines[i + 1])):
                yield i + 1, i + 1, head, ' '.join(c.strip() for c in line.strip().strip('|').split('|'))
            i += 1
            continue
        j = i + 1
        while j < n and lines[j].strip() and not HEADING.match(lines[j]) and not ITEM.match(lines[j]) \
                and not lines[j].lstrip().startswith(('```', '|')):
            j += 1
        yield i + 1, j, head, ' '.join(l.strip() for l in lines[i:j])
        i = j


def cite_lines(engine, data, starts, a, b):
    s = starts[a - 1]
    e = starts[b] if b < len(starts) else len(data)
    return engine.digest(data[s:e]), s, e - s


def build(engine, strict):
    nodes, bad, counts = [], [], {'doc_blocks': 0, 'seeds': 0}
    src = sources()
    for alt in src.get('docs', []):
        for p in expand(alt):
            name = rel(p)
            data = Path(p).read_bytes().replace(b'\r\n', b'\n')
            starts = engine.line_starts(data)
            title = os.path.basename(name)
            for a, b, head, block in md_blocks(data.decode('utf-8', 'replace')):
                if len(engine.terms(block)) < 3:
                    continue
                sha, s, ln = cite_lines(engine, data, starts, a, b)
                text = f'{title} > {head}: {block}' if head else f'{title}: {block}'
                if len(text.encode()) > TEXT_CAP:
                    text = text.encode()[:TEXT_CAP].decode('utf-8', 'ignore').rsplit(' ', 1)[0] + ' ...'
                nodes.append({'k': 'doc', 'l': 'L2', 's': [[f'{name}#L{a}-{b}', sha, s, ln]], 't': text})
                counts['doc_blocks'] += 1
    for sd in seeds():
        path = REPO / sd.get('path', '')
        why = None
        if not sd.get('path') or not path.is_file():
            why = 'file missing'
        else:
            data = path.read_bytes().replace(b'\r\n', b'\n')
            anchor = sd.get('anchor', '').encode()
            hits = data.count(anchor) if anchor else 0
            why = None if hits == 1 else f'anchor found {hits} times'
        if why:
            bad.append({'seed_line': sd['_line'], 'path': sd.get('path'), 'why': why})
            continue
        starts = engine.line_starts(data)
        a = data.count(b'\n', 0, data.find(anchor)) + 1
        b = min(a + int(sd.get('span', 1)) - 1, len(starts))
        sha, s, ln = cite_lines(engine, data, starts, a, b)
        node = {'k': sd.get('kind', 'fact'), 'l': 'L2', 's': [[f'{rel(path)}#L{a}-{b}', sha, s, ln]],
                't': f"[{sd.get('family', 'viewpoint')}] {sd['text']}"}
        if sd.get('q'):
            node['q'] = sd['q']
        nodes.append(node)
        counts['seeds'] += 1
    body = ''.join(json.dumps(n, ensure_ascii=False, sort_keys=True) + '\n' for n in nodes)
    engine.put(HERE / 'findings.jsonl', body)
    eng = Path(os.environ.get('VP_ROUTE_ENGINE', ENGINE_DEFAULT))
    manifest = {'schema': 'vp-docs-route/1', 'counts': counts, 'findings_sha16': engine.digest(body.encode()),
                'engine': {'path': str(eng), 'sha16': engine.digest(eng.read_bytes())},
                'bad_seeds': bad, 'review': 'none: findings are doc blocks and seeded facts pinned to bytes; read cited code',
                'study_pin': engine.digest(body.encode())}
    engine.put(HERE / 'manifest.json', json.dumps(manifest, indent=1, sort_keys=True) + '\n')
    (HERE / 'index' / 'meta.json').unlink(missing_ok=True)       # force an index rebuild on the next call
    return counts, bad


# ---------------------------------------------------------------- query side

def variants(word):
    out = {word}
    if word.endswith('ies') and len(word) > 4:
        out.add(word[:-3] + 'y')
    if word.endswith('es') and len(word) > 4:
        out.add(word[:-2])
    if word.endswith('s') and len(word) > 3:
        out.add(word[:-1])
    else:
        out.add(word + 's')
    return out


def ask(engine, m, query, k, only=None):
    """Engine ranking over the expanded query, then the abstention measure on the original terms.
    Returns ([(row, node)], meta); `only` filters nodes (used by `route` for the command taxonomy)."""
    groups = {w: variants(w) for w in engine.terms(query)}
    if not groups:
        return [], {'coverage': 0.0, 'absent_terms': [], 'terms': []}
    docs = m.docs()
    total = len(docs)
    df = {w: sum(1 for d in docs if d & vs) for w, vs in groups.items()}
    weight = {w: math.log(1 + (total + 1) / (1 + df[w])) for w in groups}
    expanded = ' '.join(sorted(set().union(*groups.values())))
    index = {id(n): i for i, n in enumerate(m.nodes)}
    pairs, seen, best = [], set(), 0.0
    for score, node in m.rank_nodes(expanded, {'L2'}):
        if node['t'] in seen or (only and not only(node)):
            continue
        seen.add(node['t'])
        got = [w for w, vs in groups.items() if docs[index[id(node)]] & vs]
        cover = sum(weight[w] for w in got) / sum(weight.values())
        if not pairs:
            best = cover
        row = m.row(node, score)
        row['coverage'] = round(cover, 3)
        pairs.append((row, node))
        if len(pairs) == k:
            break
    meta = {'coverage': round(best, 3), 'absent_terms': sorted(w for w in groups if df[w] == 0), 'terms': sorted(groups)}
    return pairs, meta


def coord(ref):
    """path#La-b -> path:a-b, the clickable form."""
    m = re.match(r'^(.*)#L(\d+)-(\d+)$', ref)
    return ref if not m else (f'{m.group(1)}:{m.group(2)}' if m.group(2) == m.group(3) else f'{m.group(1)}:{m.group(2)}-{m.group(3)}')


def spool(text, stem):
    state = os.environ.get('VP_OPS_STATE') or str(Path(os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir()) / 'viewpoint-ops')
    base = Path(os.environ.get('VP_SPOOL_DIR') or Path(state) / 'spool')
    base.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(text.encode()).hexdigest()
    path = base / f"{stem}-{sha[:16]}.md"
    if not path.exists():
        path.write_text(text, encoding='utf-8')
    return {'path': str(path), 'lines': text.count('\n'), 'bytes': len(text.encode()), 'sha256': sha}


def emit(text):
    """Print at most PRINT_CAP characters, whole lines; report how many were held back."""
    if not text:
        return 0
    if len(text) <= PRINT_CAP:
        print(text)
        return 0
    cut = text.rfind('\n', 0, PRINT_CAP)
    print(text[:cut if cut > 0 else PRINT_CAP])
    return len(text) - (cut if cut > 0 else PRINT_CAP)


COMMAND = re.compile(r'`vp ([a-z][a-z-]*) ([a-z][a-z-]*)`')


def commands(m, rows_nodes, k=3):
    """vp commands named by the ranked findings of the command taxonomy (design/COMMANDS.md), best first."""
    out, seen = [], set()
    for row, node in rows_nodes:
        path = m.path_of(node['s'][0][0])
        if not path.endswith('COMMANDS.md') or row['coverage'] < COVERAGE_FLOOR:
            continue
        hit = COMMAND.search(node['t'].split(': ', 1)[0]) or COMMAND.search(node['t'])
        if not hit or hit.group(0) in seen:
            continue
        seen.add(hit.group(0))
        why = node['t'].split(': ', 1)[-1][:160]
        out.append({'argv': ['vp', hit.group(1), hit.group(2)], 'why': why, 'cite': coord(row['code'][0]), 'mark': row['state']})
        if len(out) == k:
            break
    return out


def route_commands(engine, m, query, k=3):
    """Rank whole commands: every block of design/COMMANDS.md under one `vp <noun> <verb>` heading (or naming it in the
    index table) is one document. Returns (commands, meta) with the same coverage measure as `ask`."""
    groups = {w: variants(w) for w in engine.terms(query)}
    if not groups:
        return [], {'coverage': 0.0, 'absent_terms': [], 'terms': []}
    docs = m.docs()
    df = {w: sum(1 for d in docs if d & vs) for w, vs in groups.items()}
    weight = {w: math.log(1 + (len(docs) + 1) / (1 + df[w])) for w in groups}
    cmds = {}
    order = sorted(range(len(m.nodes)), key=lambda i: os.path.basename(m.path_of(m.nodes[i]['s'][0][0])).startswith('FAMILY_'))
    for i in order:                                                    # taxonomy first, then the family pages
        node = m.nodes[i]
        path = m.path_of(node['s'][0][0])
        if os.path.basename(path).startswith('FAMILY_'):               # family pages add their words to the commands they name
            for hit in COMMAND.finditer(node['t'].split(': ', 1)[-1]):
                if (hit.group(1), hit.group(2)) in cmds:
                    cmds[(hit.group(1), hit.group(2))]['words'] |= docs[i] - set(engine.words(os.path.basename(path)))
            continue
        if not path.endswith('COMMANDS.md'):
            continue
        hit = COMMAND.search(node['t'].split(': ', 1)[0]) or COMMAND.match(node['t'].split(': ', 1)[-1])
        if hit:
            entry = cmds.setdefault((hit.group(1), hit.group(2)), {'words': set(), 'nodes': []})
            entry['words'] |= docs[i]
            entry['nodes'].append(node)
    total = sum(weight.values())
    scored = sorted(((sum(weight[w] for w, vs in groups.items() if e['words'] & vs) / total, key) for key, e in cmds.items()),
                    key=lambda x: (-x[0], x[1]))
    out = []
    for cover, key in scored[:k]:
        if cover < COVERAGE_FLOOR:
            break
        heading = next((n for n in cmds[key]['nodes'] if '(class' in n['t'].split(': ', 1)[0]), cmds[key]['nodes'][0])
        row = m.row(heading)
        out.append({'argv': ['vp', *key], 'why': heading['t'].split(': ', 1)[-1][:160], 'cite': coord(row['code'][0]), 'mark': row['state'],
                    'coverage': round(cover, 3)})
    meta = {'coverage': round(scored[0][0], 3) if scored else 0.0, 'absent_terms': sorted(w for w in groups if df[w] == 0), 'terms': sorted(groups)}
    return out, meta


def skill_route(query):
    """Team skill for the intent, from newton-py's own router (`newton route`). None when it abstains or is absent."""
    npy = os.environ.get('VP_NPY', NPY)
    if not os.path.isfile(npy):
        return None
    try:
        done = subprocess.run([npy, 'route', query], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = {}
    for line in done.stdout.decode('utf-8', 'replace').splitlines()[:12]:
        key, sep, val = line.partition(': ')
        if sep and key in ('skill', 'argv', 'precondition', 'cite'):
            out[key] = val.split(' sha256=')[0][:300]
    return out if 'skill' in out else None


def main(argv=None):
    ap = argparse.ArgumentParser(prog='vp docs', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['card', 'ask', 'pack', 'locate', 'route', 'show', 'file', 'find', 'check', 'stats', 'build'])
    ap.add_argument('query', nargs='?', default='')
    ap.add_argument('--k', type=int, default=5)
    ap.add_argument('--budget', type=int, default=6000)
    ap.add_argument('--strict', action='store_true', help='build: AUDIT_VIOLATIONS when a seed cannot be pinned')
    ap.add_argument('--dry-run', action='store_true', help='print what would run; read nothing, write nothing')
    ap.add_argument('--json', action='store_true', help='print the receipt line only')
    usage = 'vp docs card|ask|pack|locate|route|show|file|find|check|stats|build [query] [--k N] [--budget N] [--dry-run] [--json]'
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        if e.code in (0, None):
            return 0
        return receipt('USAGE', {'usage': usage}, ['vp docs card'])
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    say = (lambda text: 0) if a.json else emit
    a.k = max(1, min(a.k, 20))
    a.budget = max(600, min(a.budget, 20000))
    if a.command in ('ask', 'pack', 'locate', 'route', 'show', 'file', 'find') and not a.query.strip():
        return receipt('USAGE', {'usage': usage, 'hint': f'vp docs {a.command} needs a query argument'}, ['vp docs card'])
    if a.dry_run:
        writes = ['router/findings.jsonl', 'router/manifest.json'] if a.command == 'build' else (['<spool>/pack-<sha16>.md'] if a.command == 'pack' else [])
        say(f'dry-run: vp docs {a.command} {a.query!r} (k={a.k}, budget={a.budget}); would write: {writes or "nothing"}')
        return receipt('DRY_RUN', {'argv': [sys.executable, '-I', str(HERE / 'vp_docs.py'), a.command] + ([a.query] if a.query else []),
                                   'env': ['VP_ROUTE_ENGINE', 'VP_SPOOL_DIR', 'VP_OPS_STATE', 'VP_NPY'], 'would_write': writes})
    engine, engine_path = load_engine()
    if engine is None:
        say(f'route engine not found at {engine_path}; set VP_ROUTE_ENGINE to newton-py/route/route.py')
        return receipt('DEPENDENCY', {'missing': [engine_path]}, ['read product/agent_interface/docs/AGENT_GUIDE.md'])
    install(engine)
    if a.command == 'build':
        counts, bad = build(engine, a.strict)
        m = engine.Map(REPO)
        say(f"built findings.jsonl: {counts['doc_blocks']} doc blocks, {counts['seeds']} seeded facts, {len(bad)} seeds not pinned\n"
            + '\n'.join(f"  seeds.jsonl:{b['seed_line']} {b['path']}: {b['why']}" for b in bad[:10]))
        return receipt('AUDIT_VIOLATIONS' if (bad and a.strict) else 'OK',
                       {'files': len(m.files), 'symbols': len(m.symbols), 'counts': counts, 'bad_seeds': len(bad), 'violations': bad[:10]}, ['vp docs check'])
    m = engine.Map(REPO)
    if a.command == 'card':
        card = m.card().rstrip('\n')
        held = say(card)
        return receipt('OK', {'tokens': engine.tokens(card), 'chars_held_back': held, 'card': str(HERE / 'card.md')},
                       ['vp docs ask "<question>"', 'vp docs route "<intent>"', 'vp docs pack "<task>" --budget 3000'])
    if a.command in ('ask', 'route'):
        if a.command == 'route':
            cmds, meta = route_commands(engine, m, a.query)
            skill = skill_route(a.query)
            if not cmds and not skill:
                say(f"AUSENTE — no inventar: no vp command or team skill matches this intent (best coverage {meta['coverage']}; "
                    f"absent terms: {', '.join(meta['absent_terms']) or 'none'}).")
                return receipt('AUSENTE', meta | {'question': a.query, 'commands': [], 'skill': None}, ['vp sys help', f'vp docs ask "{a.query}"'])
            say('\n'.join([f"{i}. {' '.join(c['argv'])}  [{c['mark']}] {c['cite']} (coverage {c['coverage']})\n   {c['why']}" for i, c in enumerate(cmds, 1)]
                          + ([f"skill: {skill['skill']}  argv: {skill.get('argv', '')}  cite: {skill.get('cite', '')}"] if skill else ['skill: none (newton route abstained)'])))
            return receipt('OK', meta | {'commands': cmds, 'skill': skill},
                           [' '.join(cmds[0]['argv']) + ' --dry-run', f"vp sys help {cmds[0]['argv'][1]} {cmds[0]['argv'][2]}"] if cmds else [])
        pairs, meta = ask(engine, m, a.query, 40)
        cmds = commands(m, pairs)
        covered = bool(pairs) and meta['coverage'] >= COVERAGE_FLOOR
        if not covered:
            say(f"AUSENTE — no inventar: nothing in the ViewPoint route covers this question "
                f"(best coverage {meta['coverage']}, floor {COVERAGE_FLOOR}; terms absent from the corpus: {', '.join(meta['absent_terms']) or 'none'}).")
            return receipt('AUSENTE', meta | {'question': a.query, 'near': [coord(r['code'][0]) for r, _ in pairs[:3]]},
                           [f'vp docs locate "{a.query}"', 'vp docs card'])
        rows = [r for r, _ in pairs[:a.k]]
        say('\n'.join(f"{i}. [{r['state']}] {', '.join(coord(c) for c in r['code'])} ({r['kind']}, coverage {r['coverage']})\n   {r['text']}"
                      for i, r in enumerate(rows, 1)))
        stale = any(r['state'] == 'STALE' for r in rows)
        data = meta | {'rows': [{'claim': r['text'][:240], 'cite': coord(r['code'][0]), 'mark': r['state'], 'kind': r['kind'], 'coverage': r['coverage']}
                                for r in rows], 'commands': cmds}
        return receipt('OK', data, [f"vp docs show {rows[0]['code'][0]}"] + (['vp docs build'] if stale else []))
    if a.command == 'pack':
        pairs, meta = ask(engine, m, a.query, 1)
        out = m.pack(a.query, a.budget)
        sp = spool(out['pack'] + '\n', 'pack')
        absent = not pairs or meta['coverage'] < COVERAGE_FLOOR
        head = out['pack'] if len(out['pack']) <= PRINT_CAP - 200 else out['pack'][:out['pack'].rfind('\n', 0, PRINT_CAP - 200)]
        say(head + ('\n\nAUSENTE — no inventar: the task itself is not covered by the route; the pack holds only the card and nearest findings.' if absent else ''))
        return receipt('AUSENTE' if absent else 'OK', {'tokens': out['tokens'], 'budget': out['budget'], 'spool': sp,
                       'chars_held_back': len(out['pack']) - len(head), 'coverage': meta['coverage'], 'absent_terms': meta['absent_terms']},
                       [f"vp out read {sp['path']} 1:60"])
    if a.command == 'show':
        ref = a.query if '#L' in a.query else re.sub(r':(\d+)-(\d+)$', r'#L\1-\2', re.sub(r':(\d+)$', r'#L\1-\1', a.query))
        out = m.show(ref)
        if out.get('code') is None:
            say(f"NOT_FOUND: {a.query} is not a readable citation (use path#La-b or path:a-b as printed by vp docs ask)")
            return receipt('NOT_FOUND', {'query': a.query}, ['vp docs ask "<question>"'])
        held = say(out['code'].rstrip('\n'))
        marks = sorted({r['state'] for r in m.file(out['path'], 20)['findings'] if ref in r['code']})
        return receipt('OK', {'finding': {'path': out['path'], 'lines': out.get('lines'), 'marks': marks}, 'chars_held_back': held},
                       [f"vp out read {REPO / out['path']} {(out.get('lines') or '1-60').replace('-', ':')}"] if held else [])
    if a.command == 'check':
        out = m.check()
        bad = m.manifest.get('bad_seeds', [])
        st = out['states']
        say(f"findings: fresh {st.get('FRESH', 0)} shift {st.get('SHIFT', 0)} stale {st.get('STALE', 0)} (fresh share {out['fresh_share']}); seeds not pinned at build: {len(bad)}")
        stale_files = sorted({m.path_of(n['s'][0][0]) for n in m.nodes if m.row(n)['state'] == 'STALE'})[:10] if st.get('STALE') else []
        return receipt('AUDIT_VIOLATIONS' if st.get('STALE') else 'OK',
                       {'fresh': st.get('FRESH', 0), 'shift': st.get('SHIFT', 0), 'stale': st.get('STALE', 0), 'bad_seeds': len(bad), 'violations': stale_files},
                       ['vp docs build'] if st.get('STALE') else [])
    if a.command == 'stats':
        out = m.stats()
        say(json.dumps(out, sort_keys=True))
        return receipt('OK', out)
    rows = getattr(m, a.command)(a.query, a.k)                  # locate | file | find
    if a.command == 'file':
        say('\n'.join(f"[{r['state']}] {', '.join(coord(c) for c in r['code'])}: {r['text'][:200]}" for r in rows['findings'][:a.k]))
        return receipt('OK' if rows['finding_count'] else 'AUSENTE', {'path': rows['path'], 'finding_count': rows['finding_count'], 'symbols': rows['symbols'][:20]})
    say('\n'.join(f"{r['path']} (via {', '.join(r['via'])})" if 'via' in r else f"{r['sid']} {r['kind']} L{r['lines']}" for r in rows))
    return receipt('OK' if rows else 'AUSENTE', {'rows': rows}, [f"vp docs file {rows[0]['path']}"] if rows and 'path' in rows[0] else [])


if __name__ == '__main__':
    sys.exit(main())
