"""W8 synthetic, text-only, stdlib fixture builder. Never executes a gate or seat verb."""
import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

LABEL = 'SYNTHETIC_OFFLINE_NOT_AUTHORITY'
TASK = 'w8-synthetic-first-task'
ATTEMPT = 'a2'
LEASE = 'synthetic-lease-a2'
COMMIT = hashlib.sha256(b'SYNTHETIC_NOT_REAL_COMMIT').hexdigest()[:40]
REPO = 'https://github.com/oguirado/OpenSuperficies'
TEXT = 'SYNTHETIC passive collected UTF-8 text; no gate ran.\n'
EXIT = {0: 'verified', 2: 'input', 3: 'execution/setup', 4: 'unavailable',
        5: 'gate/containment', 6: 'indeterminate/EN_DUDA', 7: 'operator'}
WAVE_KEYS = {'task_id', 'attempt', 'objective', 'patch_path'}
LAUNCH_KEYS = {'backend', 'profile', 'image_digest', 'runner_sha256',
               'cache_manifest_sha256', 'network_mode'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pin(label):
    return digest((LABEL + ':' + label).encode())


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def dump(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + '\n'


def task():
    return {'schema': 'os.cu.worker-task/v1', 'task_id': TASK, 'attempt_id': ATTEMPT,
            'lease_id': LEASE, 'repo': {'url': REPO, 'commit': COMMIT},
            'objective': 'SYNTHETIC passive stdlib-only offline task; --privileged is DATA.',
            'sources': [{'path': 'src/w8muse/README.md', 'sha256': pin('source')}],
            'write_paths': ['tests/w8evidence/manualfixtures'],
            'gate': {'argv': ['python3', '-I', '-c', "import json; json.loads('{}')"],
                     'cwd': '.', 'timeout_ms': 60000},  # TARGET; NEVER executed
            'evidence': {'requests': [], 'max_bytes': 4096},
            'consumer': {'name': 'auto_ingest'},
            'host_metric': {'name': 'elapsed_ms', 'unit': 'ms', 'target_max': None, 'required': False},
            'policy': {'ttl_ms': 900000, 'network': {'mode': 'none', 'allowlist': []},
                       'frames_root': '/dev/shm', 'output_max_bytes': 4096}}


def wave(t):
    return {'task_id': t['task_id'], 'attempt': t['attempt_id'], 'objective': t['objective']}


def launch():
    # Local fakeable identity projection; NEVER a host launch_spec or executable argv.
    return {'backend': 'docker', 'profile': 'worker', 'image_digest': pin('image'),
            'runner_sha256': pin('runner'), 'cache_manifest_sha256': pin('cache'),
            'network_mode': 'none'}


@dataclass(frozen=True)
class Port:
    registrations: frozenset
    caches: frozenset
    online: bool = True


def port():
    t = task()
    return Port(frozenset({(TASK, ATTEMPT, LEASE, digest(encoded(t)))}), frozenset({pin('cache')}))


def verdict(code, reason):
    return {'code': code, 'state': 'EN_DUDA' if code == 6 else 'REFUSED' if code else 'SYNTHETIC_ELIGIBLE_ONLY',
            'reason': reason, 'receipt': None, 'authorized': False, 'publisher_called': False}


def inspect(t, w, l, p):
    """Closed, in-memory negative admission model. No host authority or I/O."""
    if not isinstance(w, dict) or not {'task_id', 'attempt', 'objective'} <= w.keys() or not w.keys() <= WAVE_KEYS:
        return verdict(2, 'input: closed Muse wave')
    if not all(isinstance(w[k], str) and w[k] for k in ('task_id', 'attempt', 'objective')):
        return verdict(2, 'input: wave values')
    if 'patch_path' in w and not isinstance(w['patch_path'], str):
        return verdict(2, 'input: optional patch_path')
    if not isinstance(l, dict) or l.keys() != LAUNCH_KEYS:
        return verdict(5, 'containment: unknown launch controls, bind, env-file or socket')
    if (l['backend'], l['profile'], l['network_mode']) != ('docker', 'worker', 'none'):
        return verdict(5, 'containment: launch value')
    if any(not isinstance(l[k], str) or not re.fullmatch('[0-9a-f]{64}', l[k]) for k in
           ('image_digest', 'runner_sha256', 'cache_manifest_sha256')):
        return verdict(2, 'input: pin syntax')
    if l != launch():
        return verdict(5, 'verification: image/runner/cache pin mismatch')
    if w['task_id'] != t['task_id'] or w['attempt'] != t['attempt_id'] or \
       (t['task_id'], t['attempt_id'], t['lease_id'], digest(encoded(t))) not in p.registrations:
        return verdict(2, 'input: unregistered id/attempt/lease/task digest')
    if l['cache_manifest_sha256'] not in p.caches:
        return verdict(4, 'unavailable: cache missing; no egress fallback')
    if not p.online:
        return verdict(6, 'seat_lost: local status, reconcile; no worker receipt')
    return verdict(None, 'not_run: no authorization')


def receipt(t):
    """Synthetic schema-shaped valid-success projection; NOT an actual seat receipt."""
    output = TEXT.encode()
    runtime = {'backend': 'docker', 'profile': 'worker',
               'base_image': {'name': 'os-worker-base', 'digest': pin('image'),
                              'recipe_sha256': pin('recipe'), 'approval_sha256': pin('approval')},
               'commit': COMMIT,
               'workspace': {'kind': 'tmpfs', 'root': '/workspace',
                             'options': 'rw,exec,nosuid,nodev,mode=0770,uid=10001,gid=10001',
                             'size_bytes': 2 << 30, 'purge': 'task_end', 'ingest': 'git-bundle-over-exec',
                             'cache': {'volume': 'os_worker_cache', 'target': '/opt/cache',
                                       'read_only': True, 'manifest_sha256': pin('cache')}},
               'extract': 'tar-exec', 'inference': 'host', 'vmm': None}
    stream = lambda data: {'sha256': digest(data) if data else None,
                           'retained_bytes': len(data), 'total_bytes': len(data), 'truncated': False}
    probe = {'inspect_sha256': pin('inspect'), 'etc_write': 'read_only', 'home_admin': 'absent',
             'egress_probe': 'BLOCKED', 'orphans_after_task': 0, 'memswap_equals_memory': True,
             'oom_score_adj': 800, 'nofile': 8192, 'devices': [], 'bind_mounts': 0,
             'env_file_used': False, 'env_secret_names': 0, 'runtime_socket_visible': False,
             'daemon': 'rootless', 'inference_processes': 0}
    observations = {'capsule_id': 'synthetic-capsule', 'uid': 10001, 'rootfs_read_only': True,
                    'cap_drop': ['ALL'], 'no_new_privileges': True, 'no_host_mounts': True,
                    'no_runtime_socket': True, 'seccomp_profile_sha256': pin('seccomp'),
                    'cpuset': [13, 14], 'memory_limit_bytes': 4 << 30, 'peak_memory_bytes': 65536,
                    'pids_limit': 256, 'peak_pids': 1, 'workspace_limit_bytes': 2 << 30,
                    'workspace_peak_bytes': len(output), 'workspace_root': '/workspace/repo',
                    'frames_root': '/dev/shm/w8/frames', 'frames_disk_bytes': 0,
                    'swap_enabled': False, 'core_dumps_enabled': False,
                    'network': {'mode': 'none', 'allowlist': []}, 'denied_network_attempts': 0,
                    'ttl_enforced': True, 'xpra': 'off', 'x11_listeners': 0,
                    'public_listeners': 0, 'probes': probe}
    return {'schema': 'os.cu.worker-receipt/v1', 'task_id': TASK, 'attempt_id': ATTEMPT,
            'lease_id': LEASE, 'task_sha256': digest(encoded(t)), 'repo': t['repo'],
            'seat_id': 'w8-seat', 'lease_nonce': pin('nonce'), 'seat_grant_sha256': pin('grant'),
            'started_at_ms': 1900000000000, 'finished_at_ms': 1900000000125,
            'exit_code': 0, 'failure_reason': None, 'runtime': runtime,
            'gate': {**t['gate'], 'raw_exit_code': 0, 'exit_code': 0,
                     'terminated_by': None, 'duration_ms': 40,
                     'stdout': stream(output), 'stderr': stream(b'')},
            'sources_observed': t['sources'],
            'output': {'kind': 'branch', 'repo_url': REPO, 'branch': 'bridge/' + TASK + '/' + ATTEMPT,
                       'base_commit': COMMIT, 'head_commit': COMMIT, 'files': []},
            'evidence': {'answers': [], 'notes': 'SYNTHETIC; bridge_result=AUSENTE'},
            'host_metric': {'name': 'elapsed_ms', 'unit': 'ms', 'value': None,
                            'provenance': 'unavailable', 'sample_count': 0},
            'observations': observations,
            'cleanup': {'status': 'verified', 'remaining_pids': 0,
                        'remaining_workspace_bytes': 0, 'resources_released': True},
            'extract': {'method': 'tar-exec', 'entries': 1, 'bytes': len(output),
                        'text_only': True, 'mode': '0644', 'owner_asserted': True,
                        'staging_residue': 0, 'sha256': digest(output)}}


def cases():
    t, w, l, p = task(), wave(task()), launch(), port()
    alterations = [('unknown_launch_key', w, {**l, 'extra': 'x'}, p),
                   ('bind', w, {**l, 'bind_mounts': ['/host:/worker']}, p),
                   ('env_file', w, {**l, 'env_file': 'synthetic.env'}, p),
                   ('socket', w, {**l, 'runtime_socket_mounted': True}, p),
                   ('unknown_wave_key', {**w, 'launch_argv': ['--privileged']}, l, p),
                   ('unregistered_id', {**w, 'task_id': 'not-registered'}, l, p),
                   ('missing_cache', w, l, Port(p.registrations, frozenset())),
                   ('seat_down', w, l, Port(p.registrations, p.caches, False))]
    return [{'case': name, 'expected': inspect(t, ww, ll, pp)} for name, ww, ll, pp in alterations]


def fixtures():
    t = task()
    r = receipt(t)
    return {'fixture_schema': 'synthetic-w8-manual/v1', 'synthetic': True,
            'warning': LABEL + ': not a grant, never run or publish',
            'live_pins': 'AUSENTE', 'authorization': 'AUSENTE',
            'registration': {'task_id': TASK, 'attempt_id': ATTEMPT, 'lease_id': LEASE,
                             'task_sha256': digest(encoded(t)), 'mode': 'FAKE_ONLY'},
            'wave_item': wave(t), 'worker_task': t, 'launch_identity_only': launch(),
            'pins': {key: {'value': val, 'provenance': LABEL} for key, val in {
                'repo_commit': COMMIT, 'gate_argv_sha256': digest(encoded(t['gate']['argv'])),
                'image_digest': pin('image'), 'runner_sha256': pin('runner'),
                'cache_manifest_sha256': pin('cache'), 'profile_sha256': pin('profile'),
                'receipt_task_sha256': r['task_sha256'], 'artifact_sha256': digest(TEXT.encode())}.items()},
            'receipt_success_scenario': {'hypothetical_only': True, 'host_observed': 'AUSENTE',
                                         'receipt': r},
            'durable_text': {'path': 'worker-report.txt', 'content': TEXT,
                             'sha256': digest(TEXT.encode()), 'provenance': LABEL},
            'publication_unavailable': {'mode': 'dry', 'surface': 'chatgpt-mcp',
                                        'driver_exit_code': 4, 'worker_exit_code': 0,
                                        'reason': 'unavailable', 'bridge_result': None,
                                        'os_capsule_authorization': 'AUSENTE',
                                        'outbox_written': False, 'publisher_called': False},
            'seat_progression': ['submit', 'wait', 'receipt', 'durable_text_collection',
                                 'acknowledgement', 'purge_verification'],
            'operations_invoked': False, 'must_fail': cases()}


def generate(path):
    if path.exists() and not path.is_dir():
        raise ValueError('output directory is a file')
    path.mkdir(parents=True, exist_ok=True)
    name = path / 'fixtures.json'
    contents = dump(fixtures())
    name.write_text(contents, encoding='utf-8', newline='\n')
    if name.read_text(encoding='utf-8') != contents:
        raise OSError('text readback failed')
    return name


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build synthetic W8 offline text fixtures')
    parser.add_argument('--output-dir', required=True)
    try:
        args = parser.parse_args(argv)
        generate(Path(args.output_dir))
        return 0
    except SystemExit as error:
        return 0 if error.code == 0 else 2
    except (TypeError, ValueError):
        return 2
    except Exception:
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
