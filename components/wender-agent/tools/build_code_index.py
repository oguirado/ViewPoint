# @module {"id":"wa.tools.build_code_index","layer":0,"owner":"ARCH","deps":["wa.tools.anchors"],"book":"HB-WA-a0-arch"}
"""Deterministic anchor/HyperBook index; integer BM25 statistics, stdlib only.

Algorithm reference: math-router/tools/build_index_v2.py; source files are passive data.
"""
import argparse
import ast
from collections import Counter, defaultdict
from decimal import Decimal, localcontext, ROUND_FLOOR
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata

BUILDER = 'build_code_index.py/1.0'
CAP = 1572864   # per-file ceiling of out/code_index (raised from 384 KiB: the anchored code base outgrew it; RT-W cold load stays bounded)
ROOT = Path(__file__).resolve().parents[1]
SRC_GLOBS = ('wender/**/*.py', 'js/inpage/*.js', 'tools/*.py', 'tests/scripts/test_routing_integration.py')
ZONES = ['anchor', 'symbol', 'doc', 'hb', 'graph']
BPPM = dict(zip(ZONES, (300000, 300000, 750000, 750000, 300000)))
ZONE_W = dict(zip(ZONES, (3000, 2500, 2000, 1500, 800)))
STOP = set('''a about above after again against all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i if in into is it
its itself just me more most my no nor not now of off on once only or other our out over own same she should so some such than that the
their them then there these they this those through to too under until up very was we were what when where which while who whom why will
with would you your via using use used given let thus hence one two may must shall within without upon per'''.split())
EXPECTED_STAGES = ('HB-WA-a0-arch', 'HB-WA-s1-perceive', 'HB-WA-s2-act',
                   'HB-WA-s3-remember', 'HB-WA-s4-learn')
STAGE_ALIASES = {}
for _stage in EXPECTED_STAGES:
    for _alias in (_stage.lower(), _stage[6:], _stage[6:8], _stage.split('-')[-1]):
        STAGE_ALIASES[_alias] = _stage
STAGE_ALIASES['state'] = EXPECTED_STAGES[0]
OWNER_TO_STAGE = dict(zip(('ARCH', 'STATE', 'PERCEIVE', 'ACT', 'REMEMBER', 'LEARN'),
                         (EXPECTED_STAGES[0],) + EXPECTED_STAGES))
ANCHOR_RE = re.compile(r'^\s*(?:#|//) @anchor:([a-z][a-z0-9_.A-Z]*)\s*$')
MODULE_RE = re.compile(r'^(?:#|//) @module (\{.*\})\s*$')
JS_DEF = re.compile(r'^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s+(\w+)\s*\(')
CAMEL_RE = re.compile(r'([a-z0-9])([A-Z])')
TOKEN_RE = re.compile('[^0-9a-z]+')
PRIV = re.compile(r'/home/[^\s"\'<>`)]+|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|\b\d{4}-\d\d-\d\d[T ][\d:.+Z-]+|\b(?:sk-|ghp_)[A-Za-z0-9_-]+|\$\{?\w+\}?|https?://[^\s<>`)]+')


# @anchor:wa.tools.build_code_index.BuildError
class BuildError(ValueError):
    """A deterministic, path-free indexing failure."""


def _no_float(obj):
    if isinstance(obj, float):
        raise BuildError('E_FLOAT')
    if isinstance(obj, dict):
        for key, value in obj.items():
            _no_float(key)
            _no_float(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _no_float(value)


# @anchor:wa.tools.build_code_index.dumps
def dumps(obj):
    _no_float(obj)
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n'


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _text(text):
    return PRIV.sub('[redacted]', text)


# @anchor:wa.tools.build_code_index.toks
def toks(text):
    text = unicodedata.normalize('NFKC', text)
    text = CAMEL_RE.sub(r'\1 \2', text).casefold()
    out = []
    for token in TOKEN_RE.split(text):
        if len(token) <= 1 or token in STOP:
            continue
        out.append(token)
        for suffix in ('ations', 'ation', 'ion', 'ings', 'ing', 'ies', 'es', 'ed', 's'):
            if token.endswith(suffix) and len(token) > len(suffix) + 3:
                stem = token[:-len(suffix)]
                out.append(stem)
                if suffix in ('ing', 'ings', 'ation', 'ations', 'ion'):
                    out.append(stem + 'e')
                break
    return out


# @anchor:wa.tools.build_code_index.idf_ppm
def idf_ppm(n, df):
    with localcontext() as ctx:
        ctx.prec = 28
        d = Decimal(max(df, 1))
        return int(((1 + (Decimal(n) - d + Decimal('0.5')) /
                     (d + Decimal('0.5'))).ln() * 1000000).to_integral_value(rounding=ROUND_FLOOR))


# @anchor:wa.tools.build_code_index.source_files
def source_files(root):
    root = Path(root)
    return sorted({p for pattern in SRC_GLOBS for p in root.glob(pattern)
                   if p.name != '__init__.py' and p.is_file() and not p.is_symlink()})


def _read(root, rel, sources):
    path = root / rel
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise BuildError('E_SOURCE_PATH')
    raw = path.read_bytes()
    sources[rel] = _sha(raw)
    return raw.decode('utf-8')


# @anchor:wa.tools.build_code_index.scan_hyperbooks
def scan_hyperbooks(root, out_sources):
    root = Path(root)
    hb = {'books': [], 'sections': [], 'mathlinks': [], 'companion_format_sha256': None}
    mapping, stage_texts = defaultdict(list), {}
    fmt = 'hyperbooks/COMPANION_FORMAT.json'
    if not (root / fmt).exists():
        return hb, mapping, stage_texts
    config = json.loads(_read(root, fmt, out_sources))
    hb['companion_format_sha256'] = out_sources[fmt]
    for book in sorted(config['expected_books']):
        if book not in EXPECTED_STAGES:
            raise BuildError('E_STAGE')
        base = 'hyperbooks/' + book + '/'
        md = _read(root, base + 'book.md', out_sources)
        links = [json.loads(s) for s in _read(root, base + 'mathlinks.jsonl', out_sources).splitlines() if s.strip()]
        _read(root, base + 'manifest.json', out_sources)
        safe_links = [{key: _text(str(row.get(key) or '')) for key in
                       ('target_id', 'status', 'claimed_location', 'note')} for row in links]
        hb['books'].append(book)
        hb['mathlinks'].extend(safe_links)
        headings = list(re.finditer(r'^## <a id="([^"]+)"></a>(.*)$', md, re.M))
        for i, heading in enumerate(headings):
            body = md[heading.end():headings[i + 1].start() if i + 1 < len(headings) else len(md)]
            block = re.search(r'<!-- HB:BEGIN hb.section.v2 -->\s*```json\s*(\{.*?\})\s*```', body, re.S)
            if not block:
                raise BuildError('E_HB_SECTION')
            meta = json.loads(block.group(1))
            selected = [row for row in safe_links if row['target_id'] in body]
            prose = _text(re.sub(r'<!-- HB:BEGIN.*?<!-- HB:END -->', '', body, flags=re.S))
            section = {'book': book, 'section_anchor': meta['anchor'], 'file': base + 'book.md',
                       'line': md[:heading.start()].count('\n') + 1, 'title': _text(meta['title']),
                       'kind': meta['kind'], 'code_anchors': sorted(meta.get('code_anchors', [])),
                       'mathlinks': selected, 'manifest_sha256': out_sources[base + 'manifest.json'],
                       'text': prose.strip()}
            hb['sections'].append(section)
            for anchor in section['code_anchors']:
                mapping[anchor].append(section)
            # Mentions in prose also establish navigational section references.
            for anchor in re.findall(r'\bwa\.[a-zA-Z0-9_.]+', prose):
                if section not in mapping[anchor]:
                    mapping[anchor].append(section)
            if meta['anchor'] == 'hb-s0':
                stage_texts[book] = _text(md.splitlines()[0]) + ' ' + section['title'] + ' ' + prose
    return hb, mapping, stage_texts


def _walk(tree):
    """Visit AST fields directly, avoiding iter_child_nodes/deque overhead.

    Traversal order is immaterial: callers collect sets or sorted graph edges.
    Only declared fields are visited, never the analysis cache on a Module.
    """
    pending = [tree]
    for node in pending:
        yield node
        for field in node._fields:
            value = getattr(node, field, None)
            if isinstance(value, list):
                pending.extend(child for child in value if isinstance(child, ast.AST))
            elif isinstance(value, ast.AST):
                pending.append(value)


def _python(src):
    lines = src.splitlines()
    tree = ast.parse(src)
    # String spans from the AST exclude anchor-shaped docstrings without
    # tokenizing every character of the source a second time.
    tree._routing_nodes = [node for node in _walk(tree)
                           if isinstance(node, (ast.Constant, ast.JoinedStr, ast.Import, ast.ImportFrom))]
    string_lines = set()
    for node in tree._routing_nodes:
        if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))):
            string_lines.update(range(node.lineno, node.end_lineno + 1))
    anchors = {line: match.group(1) for line, text in enumerate(lines, 1)
               if line not in string_lines and (match := ANCHOR_RE.match(text))}
    nodes = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)):
            start = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', [])])
            nodes[start] = (node, 'class' if isinstance(node, ast.ClassDef) else 'function')
            if isinstance(node, ast.ClassDef):
                for method in node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        start = min([method.lineno] + [d.lineno for d in method.decorator_list])
                        nodes[start] = (method, 'method')
    return tree, nodes, anchors, lines


def _following(lines, line):
    cursor = line
    while cursor < len(lines) and (not lines[cursor].strip() or lines[cursor].lstrip().startswith(('#', '//'))):
        cursor += 1
    return cursor + 1


def _attached(lines, start):
    cursor = start - 2
    while cursor >= 0 and (not lines[cursor].strip() or lines[cursor].lstrip().startswith(('#', '//'))):
        if ANCHOR_RE.match(lines[cursor]):
            return True
        cursor -= 1
    return False


# @anchor:wa.tools.build_code_index.resolve_stage
def resolve_stage(stage):
    return STAGE_ALIASES.get(str(stage).split('#', 1)[0].casefold())


def _stage(meta, rel, sections):
    book = meta.get('book')
    if book in EXPECTED_STAGES:
        specific = sorted({s['book'] for s in sections if s['book'] != EXPECTED_STAGES[0]})
        return specific[0] if book == EXPECTED_STAGES[0] and specific else book
    for prefix, stage in (('js/inpage/', EXPECTED_STAGES[1]), ('tools/', EXPECTED_STAGES[0]),
                          ('wender/state/', EXPECTED_STAGES[0])):
        if rel.startswith(prefix):
            return stage
    for stage in EXPECTED_STAGES[1:]:
        if rel.startswith('wender/' + stage.split('-')[-1] + '/'):
            return stage
    return OWNER_TO_STAGE.get(meta.get('owner'))


def _calls(node):
    descendants = list(_walk(node))
    shadowed = {child.name for child in descendants
                if child is not node and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        shadowed.update(arg.arg for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs)
    calls = []
    for call in descendants:
        if isinstance(call, ast.Call):
            if isinstance(call.func, ast.Name) and call.func.id not in shadowed:
                calls.append((None, call.func.id))
            elif isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
                calls.append((call.func.value.id, call.func.attr))
    return calls


# @anchor:wa.tools.build_code_index.scan_code_and_callgraph
def scan_code_and_callgraph(root, hb_info, out_sources, _parsed=None):
    root = Path(root)
    hb, mapping, stage_texts = hb_info
    recs, node_map, imports, local, modules = [], {}, {}, {}, {}
    js_lines = {}
    for path in source_files(root):
        rel = path.relative_to(root).as_posix()
        src = _read(root, rel, out_sources)
        lines = src.splitlines()
        header = MODULE_RE.match(lines[0]) if lines else None
        meta = json.loads(header.group(1)) if header else {}
        if path.suffix == '.py':
            tree, nodes, anchors, lines = _python(src)
            aliases = {}
            package = rel[:-3].split('/')[:-1]
            for node in tree._routing_nodes:
                if isinstance(node, ast.ImportFrom):
                    base = '.'.join(package[:len(package) - node.level + 1] + ([node.module] if node.module else [])) if node.level else (node.module or '')
                    for alias in node.names:
                        aliases[alias.asname or alias.name] = (base, alias.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        aliases[alias.asname or alias.name.split('.')[0]] = (alias.name, None)
            imports[rel] = aliases
            if _parsed is not None:
                _parsed[rel] = _missing_in_source(rel, src, (tree, nodes, anchors, lines))
        else:
            js_lines[rel] = lines
            anchors = {i: m.group(1) for i, line in enumerate(lines, 1) if (m := ANCHOR_RE.match(line))}
            nodes = {}
            if _parsed is not None:
                _parsed[rel] = _missing_in_source(rel, src)
        for line, anchor in sorted(anchors.items()):
            start = _following(lines, line)
            node, kind = nodes.get(start, (None, 'symbol'))
            symbol, doc = anchor.rsplit('.', 1)[-1], ''
            if node is not None and hasattr(node, 'name'):
                symbol = node.name
                doc = (ast.get_docstring(node) or '').strip().split('\n')[0]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                symbol = getattr(target, 'id', symbol)
                kind = 'const'
            elif path.suffix == '.js' and start <= len(lines):
                match = JS_DEF.match(lines[start - 1]) or re.match(r'\s*(?:const|let|var)\s+(\w+)\s*=', lines[start - 1])
                if match:
                    symbol = match.group(1)
                    kind = 'js_function' if JS_DEF.match(lines[start - 1]) else 'const'
                doc = ' '.join(s.strip().lstrip('/').strip() for s in lines[line:start - 1])
            sections = mapping.get(anchor, [])
            rec = {'id': anchor, 'file': rel, 'line': line, 'symbol': symbol, 'kind': kind,
                   'stage': _stage(meta, rel, sections),
                   'hb_sections': sorted({s['book'] + '#' + s['section_anchor'] for s in sections}),
                   'layer': meta.get('layer'), 'owner': meta.get('owner'), 'module_id': meta.get('id'),
                   'doc': _text(doc)[:160], 'callers': [], 'callees': []}
            recs.append(rec)
            if anchor in node_map:
                raise BuildError('E_DUPLICATE_ANCHOR')
            node_map[anchor] = _calls(node) if node is not None else None
            local[(rel, symbol)] = anchor
            modules[(meta.get('id'), symbol)] = anchor
            modules[(rel[:-3].replace('/', '.'), symbol)] = anchor
    unique = defaultdict(list)
    for r in recs:
        unique[r['symbol']].append(r['id'])
    edges = defaultdict(set)
    for r in recs:
        calls = node_map[r['id']]
        if calls is None and r['file'].endswith('.js'):
            lines = js_lines[r['file']]
            end = next((i for i in range(r['line'], len(lines)) if ANCHOR_RE.match(lines[i])), len(lines))
            calls = [(None, name) for name in re.findall(r'\b(\w+)\s*\(', '\n'.join(lines[r['line']:end]))]
        for mod, name in calls or []:
            target = None
            aliases = imports.get(r['file'], {})
            if mod in aliases:
                target = modules.get((aliases[mod][0], name))
            elif mod is None:
                target = local.get((r['file'], name))
                if not target and name in aliases:
                    target = modules.get(aliases[name])
            if not target and len(unique[name]) == 1:
                target = unique[name][0]
            if target and target != r['id']:
                edges[r['id']].add(target)
    inverse = defaultdict(set)
    for caller, callees in edges.items():
        for callee in callees:
            inverse[callee].add(caller)
    for r in recs:
        r['callees'] = sorted(edges[r['id']])[:16]
        r['callers'] = sorted(inverse[r['id']])[:16]
    return sorted(recs, key=lambda r: (r['id'], r['file'], r['line']))


def _missing_in_source(rel, src, analysis=None):
    missing = []
    lines = src.splitlines()
    header = MODULE_RE.match(lines[0]) if lines else None
    try:
        valid_header = header and isinstance(json.loads(header.group(1)), dict)
    except ValueError:
        valid_header = False
    if not valid_header:
        missing.append(dict(file=rel, line=1, symbol='<module>', kind='module_header'))
    if rel.endswith('.py'):
        tree, nodes, anchors, lines = analysis if analysis is not None else _python(src)
        starts = {_following(lines, line) for line in anchors}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith('_'):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                if start not in starts:
                    missing.append(dict(file=rel, line=node.lineno, symbol=node.name,
                                        kind='class' if isinstance(node, ast.ClassDef) else 'function'))
    else:
        for i, line in enumerate(lines, 1):
            match = JS_DEF.match(line) if line == line.lstrip() else None
            if match and not match.group(1).startswith('_') and not _attached(lines, i):
                missing.append(dict(file=rel, line=i, symbol=match.group(1), kind='js_function'))
    return missing


# @anchor:wa.tools.build_code_index.check_missing_anchors
def check_missing_anchors(root, _parsed=None):
    missing = []
    root = Path(root)
    for path in source_files(root):
        rel = path.relative_to(root).as_posix()
        if _parsed is not None and rel in _parsed:
            missing.extend(_parsed[rel])
        else:
            missing.extend(_missing_in_source(rel, path.read_text(encoding='utf-8')))
    return sorted(missing, key=lambda r: (r['file'], r['line'], r['symbol']))


# @anchor:wa.tools.build_code_index.build_memory_index
def build_memory_index(root):
    root = Path(root)
    sources = {}
    info = scan_hyperbooks(root, sources)
    parsed = {}
    recs = scan_code_and_callgraph(root, info, sources, parsed)
    hb, mapping, stage_texts = info
    missing = check_missing_anchors(root, parsed)
    cols = {'N': len(recs), 'id': [r['id'] for r in recs], 'stage': [r['stage'] for r in recs],
            'zones': ZONES, 'B': BPPM, 'A': {}, 'len': {z: [] for z in ZONES}}
    # These caches live for one build: source changes always force fresh analysis.
    token_counts, idfs = {}, {}
    postings = defaultdict(lambda: defaultdict(list))
    for i, r in enumerate(recs):
        r['i'] = i
        hb_text = stage_texts.get(r['stage'], '')
        for s in mapping.get(r['id'], []):
            if r['id'] in s['code_anchors']:
                hb_text += ' ' + s['title'] + ' ' + s['text'] + ' ' + ' '.join(row['note'] + ' ' + row['claimed_location'] for row in s['mathlinks'])
        texts = {'anchor': r['id'], 'symbol': r['symbol'] + ' ' + r['file'], 'doc': r['doc'],
                 'hb': hb_text + ' ' + str(r['stage']) + ' ' + str(r['owner']),
                 'graph': ' '.join(a.rsplit('.', 1)[-1] for a in r['callers'] + r['callees'])}
        for z in ZONES:
            text = texts[z]
            if text not in token_counts:
                token_counts[text] = Counter(toks(text))
            counts = token_counts[text]
            cols['len'][z].append(sum(counts.values()))
            for term, tf in sorted(counts.items()):
                postings[term][z].extend((i, tf))
    cols['A'] = {z: 1000 * sum(cols['len'][z]) // max(len(recs), 1) for z in ZONES}
    post = {}
    for term, zones in sorted(postings.items()):
        df = len({i for values in zones.values() for i in values[::2]})
        if df not in idfs:
            idfs[df] = idf_ppm(len(recs), df)
        post[term] = {'d': df, 'i': idfs[df], 'z': dict(zones)}
    files = {'anchors.jsonl': ''.join(dumps(r) for r in recs).encode(),
             'hb.json': dumps(hb).encode(), 'cols.json': dumps(cols).encode(), 'post.json': dumps(post).encode()}
    hashes = {name: _sha(raw) for name, raw in sorted(files.items())}
    idx = _sha(''.join(name + '\t' + digest + '\n' for name, digest in sorted(hashes.items())).encode())
    man = {'schema': 'wa.code-index-manifest/v1', 'builder': BUILDER, 'corpus': 'wender-agent',
           'nodes': len(recs), 'hb_books': len(hb['books']), 'hb_sections': len(hb['sections']),
           'unanchored_public_count': len(missing), 'files': hashes, 'sources': sources,
           'max_file_bytes': max(map(len, files.values())), 'index_sha256': idx}
    # Include manifest size in the reported maximum (iterate until digit length settles).
    while True:
        size = max(man['max_file_bytes'], len(dumps(man).encode()))
        if size == man['max_file_bytes']:
            break
        man['max_file_bytes'] = size
    files['manifest.json'] = dumps(man).encode()
    files['ID'] = (idx + '\n').encode()
    if man['max_file_bytes'] > CAP:
        raise BuildError('E_INDEX_CAP')
    return {'anchors': recs, 'hb': hb, 'cols': cols, 'post': post, 'manifest': man,
            'missing_public': missing, 'files': files}


# @anchor:wa.tools.build_code_index.build
def build(root, out_dir):
    ix = build_memory_index(root)
    out = Path(out_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Publish individual files atomically, manifest last; readers verify all hashes.
    # A directory rename gives atomic publication for a new destination.
    tmp = Path(tempfile.mkdtemp(prefix=out.name + '.tmp-', dir=out.parent))
    try:
        for name, raw in ix['files'].items():
            (tmp / name).write_bytes(raw)
        if not out.exists():
            os.replace(tmp, out)
        else:
            for name in sorted(ix['files'], key=lambda name: name == 'manifest.json'):
                os.replace(tmp / name, out / name)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return ix['manifest']['index_sha256'], ix['manifest']


# @anchor:wa.tools.build_code_index.check_report
def check_report(root):
    missing = check_missing_anchors(root)
    return {'schema': 'wa.anchor-check.v1', 'ok': not missing, 'rc': 4 if missing else 0,
            'code': 'E_MISSING_ANCHORS' if missing else 'OK', 'missing_count': len(missing), 'missing': missing}


# @anchor:wa.tools.build_code_index.main
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--out', type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true')
    group.add_argument('--check-index', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.check:
            report = check_report(args.root)
            sys.stdout.write(dumps(report))
            return report['rc']
        out = args.out or args.root / 'out/code_index'
        if args.check_index:
            ix = build_memory_index(args.root)
            ok = all((out / name).is_file() and (out / name).read_bytes() == raw for name, raw in ix['files'].items())
            sys.stdout.write(dumps({'ok': ok, 'code': 'OK' if ok else 'E_STALE'}))
            return 0 if ok else 5
        idx, man = build(args.root, out)
        sys.stdout.write(dumps(man))
        return 0
    except BuildError as exc:
        sys.stdout.write(dumps({'code': str(exc)}))
        return 3
    except (OSError, ValueError, SyntaxError, KeyError):
        sys.stdout.write(dumps({'code': 'E_INDEX'}))
        return 3


if __name__ == '__main__':
    sys.exit(main())
