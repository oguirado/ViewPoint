#!/usr/bin/env python3
"""Regenerate synthetic fixtures; canon is read-only and is never used by Go.
Run from the repository root with python3 -B testdata/workerbridge/generate.py.
"""
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

def main():
    ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(ROOT))
    from contracts.worker.tests import test_worker_task_schema as model
    CANON = Path(os.environ.get('OS_CU_CANON_BRIDGE', '/home/user/Música/ORQUESTADOR_WAVEMIND_PROYECTOS/canon/WEB_BRIDGE/schemas')).parent / 'tools'
    sys.path.insert(0, str(CANON))
    import bridge_core as core
    import bridge_canon as canon


    def dump(name, value):
        (Path(__file__).parent / name).write_bytes(model.canonical_bytes(value) + b'\n')


    task, receipt, grant = model.bridge_fixture()
    bridge = task['bridge']
    envelope = model.encode_bridge_result(task, receipt, 'PROMPT-ID synthetic', live=False)
    result = envelope['result']
    now = datetime.datetime(2026, 10, 20, tzinfo=datetime.timezone.utc)
    lease = {k: bridge[k] for k in ('thread', 'agent', 'worker_id', 'task_id', 'attempt_id', 'key', 'base_seq', 'expires_at_utc')}
    lease.update(status='granted', task_sha256=hashlib.sha256(canon.dumps(bridge)).hexdigest())
    expected = [{'request_index': i, 'kind': 'text', 'expected': None} for i in range(4)]
    base = dict(task=bridge, result=result, report='Synthetic gate report.\n', lease=lease, head=0,
                now=now.strftime('%Y-%m-%dT%H:%M:%SZ'), changes=[{'status': 'M', 'mode': '100644', 'paths': ['src/work.go']}],
                expected=expected)


    class Ledger:
        keys = {}
        receipts = {}
        seq = 0
        def __init__(self, fixture):
            self.leases = {} if fixture['lease'] is None else {fixture['task']['lease_id']: fixture['lease']}
            self.fixture = fixture
        def head(self, thread):
            return self.fixture['head']


    cases = []
    def case(name, mutate=lambda c: None):
        c = copy.deepcopy(base)
        mutate(c)
        verdict = core.check(Ledger(c), c['task'], c.get('result_wire', canon.dumps(c['result']).decode()).encode(), c['report'].encode(),
                             changes=c['changes'], expected=c['expected'], now=now)
        c.update(name=name, status=verdict['status'], reject_class=verdict['cls'])
        cases.append(c)

    case('accept')
    case('unicode-report', lambda c: c.update(report='Synthetic café 水 😀 \u2028.'))
    case('missing-lease', lambda c: c.update(lease=None))
    case('expired', lambda c: c['lease'].update(expires_at_utc='2026-10-19T00:00:00Z'))
    case('revoked', lambda c: c['lease'].update(status='revoked'))
    case('stale-head', lambda c: c.update(head=1))
    case('absent-head', lambda c: c.update(head=None))
    case('stale-base', lambda c: c['result'].update(base_seq=1))
    for key, val in [('task_id','other-task'),('thread','other-thread'),('agent','codex'),('worker_id','other-worker'),
                     ('attempt_id','a2'),('lease_id','lease-other'),('key','other-key'),('done_signal','READY other'),
                     ('surface','chatgpt-app'),('role','reviewer')]:
        case('binding-'+key, lambda c,k=key,v=val:c['result'].update({k:v}))
    case('requested-ref', lambda c:c['result']['source_binding'].update(requested_ref='other/ref'))
    case('report-path', lambda c:c['result']['content'].update(path='other.md'))
    case('unknown-key', lambda c:c['result'].update(extra=True))
    case('missing-key', lambda c:c['result'].pop('task_id'))
    case('empty-report', lambda c:c.update(report=' \n'))
    case('missing-evidence', lambda c:c['result'].update(evidence_answers=[]))
    case('duplicate-evidence', lambda c:c['result']['evidence_answers'].append(c['result']['evidence_answers'][0]))
    case('out-of-range-evidence', lambda c:c['result']['evidence_answers'][0].update(request_index=99))
    case('artifact-outside', lambda c:c['result']['artifacts'].append({'path':'outside.go','sha256':None}))
    case('change-outside', lambda c:c['changes'][0].update(paths=['outside.go']))
    case('symlink', lambda c:c['changes'][0].update(mode='120000'))
    case('submodule', lambda c:c['changes'][0].update(mode='160000'))
    case('inbox', lambda c:c['changes'][0].update(paths=['.wavemind/bridge/inbox/other/task.json']))
    case('no-substantive-change', lambda c:c.update(changes=[]))
    case('content-digest', lambda c:c['result']['content'].update(sha256='0'*64))
    case('evidence-mismatch', lambda c:c['expected'][0].update(kind='sha256', expected='0'*64))
    def blocked(c):
        for a in c['result']['evidence_answers']:a['answer']='ABSENT'
        c['result']['source_binding']['evidence']='BLOCKED: gate failed'
        c['changes']=[]
    case('blocked', blocked)
    case('negated-blocker', lambda c:(blocked(c), c['result']['source_binding'].update(evidence='not blocked')))
    for name, wire in [
        ('wire-duplicate', '{"x":1,"x":2}'),
        ('wire-fraction', '{"x":1.0}'),
        ('wire-exponent', '{"x":1e0}'),
        ('wire-negative-zero', '{"x":-0}'),
        ('wire-nan', '{"x":NaN}'),
        ('wire-infinity', '{"x":Infinity}'),
        ('wire-neg-infinity', '{"x":-Infinity}'),
        ('wire-surrogate', r'{"x":"\ud800"}'),
        ('wire-trailing', '{} {}'),
        ('wire-array', '[]'),
        ('wire-bom', '\ufeff{}'),
    ]:
        case(name, lambda c,w=wire: c.update(result_wire=w))
    dump('canon_cases.json', cases)
    # Independent golden projection and encoding, including byte-level canonical vectors.
    dump('codec.json', dict(bridge=bridge, worker=task, receipt=receipt, grant=grant, envelope=envelope,
                           worker_bytes=model.canonical_bytes(task).decode(), bridge_bytes=canon.dumps(bridge).decode(),
                           envelope_bytes=model.canonical_bytes(envelope).decode()))
    mutations = {
     'bind_mount': {'binds':['/home/user:/host']},
     'bind_as_volume': {'volumes':[{'source':'/home/user/.config','target':'/opt/cache','read_only':True}]},
     'env_file': {'env_file':'/home/user/.env'},
     'docker_sock_volume': {'volumes':[{'source':'/var/run/docker.sock','target':'/var/run/docker.sock','read_only':True}]},
     'docker_sock_mounts': {'mounts':[{'type':'bind','source':'/var/run/docker.sock'}]},
     'secret_env': {'env_names':['ANTHROPIC_API_KEY']}, 'privileged': {'privileged':True},
     'host_network': {'network':'host'}, 'writable_root': {'read_only':False}, 'caps': {'cap_drop':[]},
     'swap': {'memswap_bytes':536870912}, 'oom_low': {'oom_score_adj':0},
     'interactive_core': {'cpuset':[3,10]}, 'kvm_in_docker': {'devices':['/dev/kvm']},
     'tmpfs_suid': {'tmpfs':[{'target':'/workspace','options':'rw,exec,suid,dev'}]},
     'no_workspace': {'tmpfs':[{'target':'/tmp','options':'rw,noexec,nosuid,nodev'}]},
     'cache_writable': {'volumes':[{'source':'os_worker_cache','target':'/opt/cache','read_only':False}]}}
    launch=[]
    for name, backend, change in [('docker','docker',{}),('microvm','microvm',{'devices':['/dev/kvm']}),
                                   ('microvm-missing-kvm','microvm',{}), *[(k,'docker',v) for k,v in mutations.items()]]:
        spec={**copy.deepcopy(model.LAUNCH_SPEC),**change}
        launch.append(dict(name=name, backend=backend, interactive_cpus=list(range(10)), spec=spec,
                           rc=model.launch_admission(spec,backend,list(range(10)))))
    dump('launch_cases.json',launch)
    vectors=[]
    for value in [None,True,123456789012345678901234567890,{'z':'é水😀\u2028\u2029<>/&','a':'\x00\b\f\n\r\t\x1f'},['x',0,-2]]:
        vectors.append(dict(value=value, worker=model.canonical_bytes(value).decode(), bridge=canon.dumps(value).decode()))
    dump('canonical.json',vectors)
    dump('provenance.json', {'bridge_core_sha256':hashlib.sha256((CANON/'bridge_core.py').read_bytes()).hexdigest(),
                            'model_sha256':hashlib.sha256(Path(model.__file__).read_bytes()).hexdigest(),
                            'paired_cases':len(cases),'launch_cases':len(launch)})
    print(f'generated canon={len(cases)} launch={len(launch)}')

    # Capture the co-signed tests' calls to the schema interpreter. Cross-document
    # assertions remain in Python; Go independently checks schema verdicts only.
    import io
    import unittest
    schema_cases = {}
    original_validate = model.ENGINE.validate

    def capture(instance, definition=None):
        key = (definition or '', model.canonical_bytes(instance))
        try:
            original_validate(instance, definition)
        except model.ValidationError:
            schema_cases[key] = dict(definition=definition or '', instance=copy.deepcopy(instance), valid=False)
            raise
        else:
            schema_cases[key] = dict(definition=definition or '', instance=copy.deepcopy(instance), valid=True)

    model.ENGINE.validate = capture
    try:
        suite = unittest.defaultTestLoader.loadTestsFromModule(model)
        log = io.StringIO()
        outcome = unittest.TextTestRunner(stream=log).run(suite)
        if not outcome.wasSuccessful() or outcome.skipped:
            raise RuntimeError('co-signed test capture failed or skipped')
    finally:
        model.ENGINE.validate = original_validate
    dump('schema_cases.json', [schema_cases[k] for k in sorted(schema_cases)])
    print(f'captured schema cases={len(schema_cases)}')


if __name__ == '__main__':
    if len(sys.argv) != 1:
        print('usage: python3 -B testdata/workerbridge/generate.py', file=sys.stderr)
        raise SystemExit(2)
    try:
        main()
    except KeyboardInterrupt:
        print('E_GATE fixture_generation interrupted', file=sys.stderr)
        raise SystemExit(6) from None
    except Exception as error:
        print('E_GATE fixture_generation ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(5) from None
