# @module {"id":"wa.tools.route_code","layer":0,"owner":"ARCH","deps":["wa.tools.build_code_index"],"book":"HB-WA-a0-arch"}
"""Deterministic integer BM25 code and HyperBook router.

Algorithm reference: math-router/tools/route_v2.py. No cache writes on a query.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

if __package__:
    from . import build_code_index as builder
else:
    import build_code_index as builder

VERSION = 'route_code.py/1.0.0'
K = 1200000
BUDGET = 2000
ZONE_W = builder.ZONE_W
LIM = {'id': 64, 'file': 64, 'stage': 24, 'symbol': 40, 'why': 24, 'doc': 120}


# @anchor:wa.tools.route_code.BudgetError
class BudgetError(ValueError):
    """The response cannot fit in the requested byte budget."""


# @anchor:wa.tools.route_code.IndexError
class IndexError(ValueError):
    """An index is stale or invalid."""


# @anchor:wa.tools.route_code.clip
def clip(text, size):
    return str(text).encode('utf-8')[:size].decode('utf-8', errors='ignore')


def _bad_float(value):
    raise IndexError('E_INDEX')


def _json(raw):
    return json.loads(raw, parse_float=_bad_float, parse_constant=_bad_float)


# @anchor:wa.tools.route_code.load_index
def load_index(root, index_dir=None):
    root = Path(root)
    directory = Path(index_dir) if index_dir is not None else root / 'out/code_index'
    if index_dir is None and not (directory / 'manifest.json').exists():
        return builder.build_memory_index(root)
    try:
        man = _json((directory / 'manifest.json').read_bytes())
        if man['schema'] != 'wa.code-index-manifest/v1' or man['builder'] != builder.BUILDER:
            raise IndexError('E_INDEX')
        names = {'anchors.jsonl', 'hb.json', 'cols.json', 'post.json'}
        if set(man['files']) != names:
            raise IndexError('E_INDEX')
        raw = {name: (directory / name).read_bytes() for name in sorted(names)}
        for name, payload in raw.items():
            if len(payload) > builder.CAP or hashlib.sha256(payload).hexdigest() != man['files'][name]:
                raise IndexError('E_INDEX')
        idx = hashlib.sha256(''.join(name + '\t' + digest + '\n' for name, digest in sorted(man['files'].items())).encode()).hexdigest()
        if idx != man['index_sha256'] or (directory / 'ID').read_text() != idx + '\n':
            raise IndexError('E_INDEX')
        # Discover the source inventory anew, so additions and deletions invalidate a cache.
        sources = {}
        builder.scan_hyperbooks(root, sources)
        for path in builder.source_files(root):
            rel = path.relative_to(root).as_posix()
            builder._read(root, rel, sources)
        if sources != man['sources']:
            raise IndexError('E_STALE')
        return {'anchors': [_json(line) for line in raw['anchors.jsonl'].splitlines()],
                'hb': _json(raw['hb.json']), 'cols': _json(raw['cols.json']),
                'post': _json(raw['post.json']), 'manifest': man}
    except IndexError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise IndexError('E_INDEX') from None


# @anchor:wa.tools.route_code.zone_x
def zone_x(ix, zone, d, tf, w_f):
    cols = ix['cols']
    avg = cols['A'][zone]
    ratio = cols['len'][zone][d] * 1000000000 // avg if avg > 0 else 1000000
    b = cols['B'][zone]
    norm = 1000000 - b + b * ratio // 1000000
    return w_f * tf * 1000000000 // max(norm, 1)


def _in_stage(row, canonical, section=None):
    if section is not None:
        return canonical + '#' + section in row['hb_sections']
    return row['stage'] == canonical or any(s.startswith(canonical + '#') for s in row['hb_sections'])


# @anchor:wa.tools.route_code.rank_query
def rank_query(ix, q, stage_filter=None):
    qt = sorted(set(builder.toks(q)))
    stage = builder.resolve_stage(stage_filter) if stage_filter is not None else None
    if stage_filter is not None and stage is None:
        return [], qt
    section = str(stage_filter).split('#', 1)[1] if stage_filter and '#' in stage_filter else None
    scores, hits = {}, {}
    for term in qt:
        posting = ix['post'].get(term)
        if not posting:
            continue
        accumulated = {}
        for zone, values in posting['z'].items():
            for d, tf in zip(values[::2], values[1::2]):
                accumulated[d] = accumulated.get(d, 0) + zone_x(ix, zone, d, tf, ZONE_W[zone])
        for d, a in accumulated.items():
            scores[d] = scores.get(d, 0) + posting['i'] * a // (K + a)
            hits.setdefault(d, set()).add(term)
    for d, row in enumerate(ix['anchors']):
        if row['id'] == q.strip():
            scores[d] = scores.get(d, 0) + 10000000
        if any(t in (row['symbol'].casefold(), row['id'].rsplit('.', 1)[-1].casefold()) for t in qt):
            scores[d] = scores.get(d, 0) + 500000
    ranked = []
    for d, score in scores.items():
        row = ix['anchors'][d]
        if stage_filter is not None and not _in_stage(row, stage, section):
            continue
        ranked.append(dict(row, score=score, matched=sorted(hits.get(d, set()))))
    ranked.sort(key=lambda row: (-row['score'], -len(row['matched']), row['id'], row['file'], row['line']))
    return ranked, qt


# @anchor:wa.tools.route_code.why_for
def why_for(ix, row, qt):
    terms = sorted(set(qt) & set(row.get('matched', [])), key=lambda t: (-ix['post'][t]['i'], t))
    why = []
    for term in terms[:3]:
        zones = ix['post'][term]['z']
        zone = max((z for z in zones if row['i'] in zones[z][::2]), key=lambda z: ZONE_W[z])
        why.append(clip(term + ':' + zone, LIM['why']))
    return why


# @anchor:wa.tools.route_code.build_rows
def build_rows(items, n_prot, mk_env, budget):
    """Two-pass full/base row packer, reserving protected-overflow bytes."""
    def size(rows, overflow):
        return len(builder.dumps(mk_env(rows, overflow)).encode())

    def pack(overflow):
        rows = []
        for i, (base, full) in enumerate(items):
            for candidate in (full, base):
                if size(rows + [candidate], overflow) <= budget:
                    rows.append(candidate)
                    break
            else:
                return rows, i < n_prot
        return rows, False

    if size([], False) > budget:
        raise BudgetError('E_BUDGET')
    rows, overflow = pack(False)
    if overflow:
        if size([], True) > budget:
            raise BudgetError('E_BUDGET')
        rows, _ = pack(True)
    return rows, overflow


def _envelope(mode, reason, rows, flag=False, **extra):
    return dict(schema='wa.route.v1', mode=mode, abstain=dict(flag=flag, reason=reason), r=rows, **extra)


def _response(mode, reason, rows, budget, flag=False, **extra):
    items = [({k: row[k] for k in ('id', 'file', 'line', 'stage', 'score', 'why') if k in row}, row) for row in rows]
    mk_env = lambda packed, overflow: _envelope(mode, reason, packed, flag, overflow=overflow, **extra)
    packed, overflow = build_rows(items, 1, mk_env, budget)
    return mk_env(packed, overflow), 3 if flag else 0



# @anchor:wa.tools.route_code.lookup_anchor
def lookup_anchor(ix, anchor_id, budget=BUDGET):
    rows = [dict(row, why=['exact_anchor']) for row in ix['anchors'] if row['id'] == anchor_id]
    return _response('anchor', 'exact_anchor' if rows else 'anchor_not_found', rows, budget, not rows)


# @anchor:wa.tools.route_code.list_stage
def list_stage(ix, stage_arg, k=20, budget=BUDGET):
    stage = builder.resolve_stage(stage_arg)
    section = stage_arg.split('#', 1)[1] if '#' in stage_arg else None
    sections = [s for s in ix['hb']['sections'] if s['book'] == stage and (not section or s['section_anchor'] == section)]
    if stage is None or (section and not sections):
        return _response('stage', 'stage_not_found', [], budget, True)
    selected = sorted((r for r in ix['anchors'] if _in_stage(r, stage, section)),
                      key=lambda r: (r['layer'] if r['layer'] is not None else -1, r['file'], r['line'], r['id']))
    rows = [dict(r, why=['stage:' + stage]) for r in selected[:k]]
    # Companion coordinates share the same envelope budget as code coordinates.
    companions = [{'id': s['book'] + '#' + s['section_anchor'], 'file': s['file'], 'line': s['line']} for s in sections]
    return _response('stage', 'stage', rows, budget, hb_sections=companions)


# @anchor:wa.tools.route_code.route_query
def route_query(ix, q, k=5, budget=BUDGET, stage_filter=None, no_abstain=False):
    if stage_filter is not None and builder.resolve_stage(stage_filter) is None:
        return _response('query', 'stage_not_found', [], budget, True)
    ranked, qt = rank_query(ix, q, stage_filter)
    if not qt or not ranked:
        reason = 'no_query_term' if not qt else 'no_candidate'
        return _response('query', reason, [], budget, True, m=0, cov=0)
    matched = set(t for r in ranked[:k] for t in r['matched'])
    total = sum(ix['post'].get(t, {}).get('i', builder.idf_ppm(ix['cols']['N'], 1)) for t in qt)
    coverage = sum(ix['post'][t]['i'] for t in matched) * 1000000 // max(total, 1)
    rows = []
    for r in ranked[:k]:
        row = {key: r[key] for key in ('id', 'file', 'line', 'symbol', 'stage', 'score', 'doc')}
        row.update(why=why_for(ix, r, qt), callers=r['callers'][:4], callees=r['callees'][:4])
        rows.append(row)
    flag = not matched and not no_abstain
    return _response('query', 'low_term_match' if flag else 'matched', [] if flag else rows,
                     budget, flag, m=len(matched), cov=coverage)


# @anchor:wa.tools.route_code.check_anchors_mode
def check_anchors_mode(root, budget=None):
    report = builder.check_report(root)
    return report, report['rc']


# @anchor:wa.tools.route_code.main
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--q')
    parser.add_argument('--anchor')
    parser.add_argument('--stage')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--k', type=int)
    parser.add_argument('--budget', type=int, default=BUDGET)
    parser.add_argument('--index', type=Path)
    parser.add_argument('--root', type=Path, default=builder.ROOT)
    parser.add_argument('--no-abstain', action='store_true')
    try:
        args = parser.parse_args(argv)
        if not any((args.q is not None, args.anchor, args.stage, args.check)) or (args.k is not None and not 1 <= args.k <= 50):
            raise ValueError('E_USAGE')
        if (args.anchor is not None and (args.q is not None or args.stage or args.check)) or (args.check and (args.q is not None or args.stage)):
            raise ValueError('E_USAGE')
        if args.check:
            env, rc = check_anchors_mode(args.root)
        else:
            ix = load_index(args.root, args.index)
            if args.anchor:
                env, rc = lookup_anchor(ix, args.anchor, args.budget)
            elif args.q is not None:
                env, rc = route_query(ix, args.q, args.k or 5, args.budget, args.stage, args.no_abstain)
            else:
                env, rc = list_stage(ix, args.stage, args.k or 20, args.budget)
        sys.stdout.write(builder.dumps(env))
        return rc
    except BudgetError:
        # An error envelope cannot itself fit an impossible budget; emit no bytes.
        sys.stderr.write('E_BUDGET\n')
        return 6
    except IndexError as exc:
        sys.stdout.write(builder.dumps({'code': str(exc)}))
        return 5 if str(exc) == 'E_STALE' else 3
    except (builder.BuildError, OSError, SyntaxError):
        sys.stdout.write(builder.dumps({'code': 'E_INDEX'}))
        return 3
    except (ValueError, KeyError, TypeError) as exc:
        usage = str(exc) == 'E_USAGE'
        sys.stdout.write(builder.dumps({'code': 'E_USAGE' if usage else 'E_INDEX'}))
        return 2 if usage else 3


if __name__ == '__main__':
    sys.exit(main())
