"""Execute bound Python mutation patches in lane scratch; compilation errors never count as kills.

Usage: PYTHONDONTWRITEBYTECODE=1 python3 tests/mutants/vmem/run.py
No writes to production sources; every child is timeout bounded. Each test is green on
an unmutated copy before any mutant runs. C/GDScript domain gates do not accept Python.
"""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SCRATCH = ROOT / 'build' / 'room_V2' / 'mutants'
ORACLE = '''import json,sys,unittest
sys.path.insert(0,sys.argv[1])
suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[2:])
def valid(s):
 return all(valid(x) if isinstance(x,unittest.TestSuite) else x.__class__.__name__ != '_FailedTest' for x in s)
loaded=valid(suite)
r=unittest.TestResult()
suite.run(r)
fails=len(r.failures)+len(r.errors)
print(json.dumps({'loaded':loaded,'checks':r.testsRun,'fails':fails,'details':[(str(t),s[-800:]) for t,s in r.failures+r.errors]}))
sys.exit(1 if fails else 0)
'''


def oracle(work, tests):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    p = subprocess.run([sys.executable, '-c', ORACLE, str(work), *tests], cwd=work,
                       env=env, capture_output=True, text=True, timeout=90)
    try:
        data = json.loads(p.stdout.splitlines()[-1])
    except (ValueError, IndexError):
        data = {'loaded':False,'checks':0,'fails':0,'details':p.stderr[-800:]}
    return p.returncode, data


def main():
    rows = json.loads((HERE / 'manifest.json').read_text())['mutants']
    SCRATCH.mkdir(parents=True, exist_ok=True)
    experiment = tempfile.TemporaryDirectory(prefix='run_', dir=SCRATCH)
    work = Path(experiment.name) / 'repo'
    work.mkdir()
    for name in ('vmem','visioncore','tests'):
        shutil.copytree(ROOT / name, work / name, ignore=shutil.ignore_patterns('__pycache__'))
    rc, control = oracle(work, sorted({r['test'] for r in rows}))
    if rc or not control['loaded'] or control['checks'] < 1:
        print(json.dumps({'status':'CONTROL_FAILED','rc':rc,'oracle':control}))
        return 2
    results = []
    for row in rows:
        path = work / row['file']
        original = path.read_text()
        changed = original
        for edit in row['edits']:
            if changed.count(edit['before']) != 1:
                raise ValueError('nonunique mutation anchor: '+row['id'])
            changed = changed.replace(edit['before'], edit['after'], 1)
        if changed == original:
            raise ValueError('inert mutant: '+row['id'])
        ast.parse(changed)
        try:
            path.write_text(changed)
            rc, result = oracle(work, [row['test']])
            killed = rc == 1 and result['loaded'] and result['checks'] > 0 and result['fails'] > 0
            results.append({'id':row['id'],'module':path.stem,'killed':killed,'oracle_rc':rc,**result})
        finally:
            path.write_text(original)
    receipt = {'schema':'vmem.mutants/v1','control_checks':control['checks'],
               'total':len(results),'killed':sum(r['killed'] for r in results),'results':results}
    receipt_path = SCRATCH / ('receipt_%d.json' % os.getpid())
    receipt_path.write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(dict({k:v for k,v in receipt.items() if k!='results'}, receipt=str(receipt_path))))
    experiment.cleanup()
    for row in results:
        if not row['killed']:
            print(json.dumps(row))
    return 0 if all(r['killed'] for r in results) else 5


if __name__ == '__main__':
    sys.exit(main())
