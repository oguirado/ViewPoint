"""Consume pending NONE EXRs with unchanged bpy-side quantisers, on A725 CPUs.

Pending bvqa.truth-pending/v1 rows contain frame, passes (truth key to EXR
path), valid_ob, valid_ma and frame_line (the truth-frame record).
Publication precedes EXR deletion; any failed wave retains its inputs.
"""
import argparse
import json
import os
from pathlib import Path

from bpy_side import decode_exr as dx
from . import exr_none

DECODE_CPUS = frozenset(range(0, 5)) | frozenset(range(10, 14))


def parse_cpus(text):
    """'5-9,12' -> frozenset; ValueError on a malformed list."""
    out = set()
    for part in str(text).split(','):
        lo, _, hi = part.partition('-')
        lo, hi = int(lo), int(hi or lo)
        if not 0 <= lo <= hi < 1024:
            raise ValueError('E_AFFINITY: bad cpu list %r' % text)
        out.update(range(lo, hi + 1))
    return frozenset(out)


def decode_row(row, base):
    if row.get('schema') != 'bvqa.truth-pending/v1' or row.get('exr_codec') != 'NONE':
        raise ValueError('E_PENDING: schema or codec')
    ri = row['frame']
    if type(ri) is not int or ri < 0:
        raise ValueError('E_PENDING: render_index')
    meta = dict(row['frame_line'])
    if meta.get('render_index') != ri or meta.get('schema') != 'bvqa.truth-frame/v1':
        raise ValueError('E_PENDING: metadata identity')
    paths = row['passes']
    if set(paths) not in (set(dx.PASSES), set(dx.PASSES)-{'uv'}):
        raise ValueError('E_PENDING: pass set')
    arrays = {}
    sources = []
    for key, name in paths.items():
        path = Path(name)
        path = path if path.is_absolute() else Path(base) / path
        rgba = exr_none.load_rgba(path)
        if rgba.shape[:2] != (meta['height'], meta['width']):
            raise ValueError('E_PENDING: dimensions')
        if key in ('ob', 'ma'):
            arrays[key] = dx.quant_index(rgba, row['valid_' + key], dx.PASSES[key])
        else:
            arrays[key] = {'mv': dx.quant_motion, 'z': dx.quant_depth,
                           'n': dx.quant_normal, 'uv': dx.quant_uv}[key](rgba)
        sources.append(path)
    return ri, meta, arrays, sources


def consume(pending, truth_dir, *, delete_exr=True, pin=True, cpus=DECODE_CPUS):
    pending, truth_dir = Path(pending), Path(truth_dir)
    if pin:
        allowed = os.sched_getaffinity(0) & frozenset(cpus)
        if not allowed:
            raise ValueError('E_AFFINITY: no admitted A725 CPU')
        os.sched_setaffinity(0, allowed)
    rows = [json.loads(line) for line in pending.read_text().splitlines() if line.strip()]
    if not rows:
        raise ValueError('E_PENDING: empty wave')
    ids = [r['frame'] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError('E_PENDING: duplicate frame')
    # Keep at most one decoded frame resident. A wave is bounded by the producer.
    if len(rows) > 8:
        raise ValueError('E_PENDING: wave exceeds eight frames')
    truth_dir.mkdir(parents=True, exist_ok=True)
    target = truth_dir / 'frames.jsonl'
    merged = {}
    if target.exists():
        for line in target.read_text().splitlines():
            if line.strip():
                old = json.loads(line)
                merged[old['render_index']] = old
    sources = []
    for row in rows:
        ri, meta, arrays, paths = decode_row(row, pending.parent)
        meta['files'] = dx.write_frame(arrays, str(truth_dir), ri)
        merged[ri] = meta
        sources.extend(paths)
    content = ''.join(json.dumps(merged[k], sort_keys=True, separators=(',', ':'))+'\n' for k in sorted(merged))
    tmp = target.with_suffix('.jsonl.tmp')
    tmp.write_text(content)
    os.replace(tmp, target)
    if delete_exr:
        for path in set(sources):
            path.unlink()
        pending.unlink()
    return {'frames': len(rows), 'status': 'COMPLETE'}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--pending', required=True)
    ap.add_argument('--truth-dir', required=True)
    ap.add_argument('--keep-exr', action='store_true')
    ap.add_argument('--cpus', default=None, help='admitted CPU list (host reservation, e.g. 5-9); default A725 0-4,10-13')
    a = ap.parse_args(argv)
    try:
        result = consume(a.pending, a.truth_dir, delete_exr=not a.keep_exr,
                         cpus=DECODE_CPUS if a.cpus is None else parse_cpus(a.cpus))
    except (OSError, ValueError, KeyError, TypeError, dx.TruthError) as exc:
        print(json.dumps({'code': 'E_DECODE', 'error': str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
