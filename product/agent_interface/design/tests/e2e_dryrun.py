"""Dry-run every `vp` line of E2E_SCENARIOS.md through the real CLI (lane ops-cli). Starts nothing.

    python3 -I tests/e2e_dryrun.py   # run from design/; VP path below
"""
import json, os, shlex, subprocess, sys, collections
sys.path.insert(0, '.')
import validate_cmd as V
_AI = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VP = next((p for p in (os.path.join(_AI, 'cli', 'vp'), os.path.join(os.environ.get('VP_OPS_ROOT') or os.path.normpath(os.path.join(_AI, '..', '..', '..')), 'vp-wt-fable-ai2', 'product', 'agent_interface', 'cli', 'vp')) if os.path.isfile(p)), 'vp')
env = dict(os.environ, J='vpops-demo1', RT='/run/user/1000/viewpoint-ops/rt/vpops-demo1', E=os.path.join(__import__('tempfile').gettempdir(), 'vpops-e2e-evidence', 'vpops-demo1'), ROOM='vpops-demo1', XDG_RUNTIME_DIR='/run/user/1000')
sub = {'<display>': ':99', '<pid>': '999999', '<spool>': '/etc/hostname', '<frame>': '3', '<handoff_id>': 'director-1', '<point>': 'p1'}
res = collections.Counter(); bad = []
for n, line in V.sh_lines('E2E_SCENARIOS.md'):
    for k, v in sub.items(): line = line.replace(k, v)
    line = os.path.expandvars(line) if False else line
    argv = [os.path.expandvars(a) if True else a for a in shlex.split(line)]
    argv = [a.replace('$J', env['J']).replace('${J}', env['J']).replace('$RT', env['RT']).replace('$E', env['E']).replace('$ROOM', env['ROOM']) for a in shlex.split(line)]
    if '--dry-run' not in argv: argv.append('--dry-run')
    p = subprocess.run([VP] + argv[1:], capture_output=True, text=True, timeout=60, env=env)
    last = (p.stdout.strip().splitlines() or [''])[-1]
    try: r = json.loads(last); code = r.get('code')
    except ValueError: r = {}; code = 'NO_RECEIPT'
    res[code] += 1
    if code != 'DRY_RUN' or p.returncode != 0:
        bad.append((n, ' '.join(argv[1:3]), code, p.returncode, json.dumps(r.get('data'))[:160]))
print(dict(res)); [print(b) for b in bad]
