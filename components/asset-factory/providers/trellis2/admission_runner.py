"""Issue a permit and execute one approved, offline native Docker workload.

All Docker CLI children use the existing build.run_owned PID-namespace runner;
the daemon-owned container is addressed only by its recorded full ID. No raw
signals, background model calls, credential reads, image pulls or downloads.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import os
from pathlib import Path
import sys
import time

from asset_factory import core
from . import admission as a
from .adapter import Refusal
from .build.build import run_owned
from tools.scheduler import scheduler

MODEL_LEASE = Path('/var/lib/viewpoint/asset-factory/model-execution.lock')
FACTORY = Path(__file__).resolve().parents[2]


def docker(*args, timeout_s=30):
    return run_owned(['docker', *map(str, args)], timeout_s=timeout_s).decode().strip()


def inspect_container(cid):
    import json
    rows = json.loads(docker('inspect', cid))
    a.need(type(rows) is list and len(rows) == 1 and rows[0]['Id'] == cid,
           3, 'runtime_container_unproven')
    return rows[0]


def _peer_owned(pid, cid, image_id, ceiling):
    value = inspect_container(cid)
    initial = value['State']['Pid']
    a.need(value['Image'] == image_id and value['Name'].startswith('/af-') and
           value['State']['Running'] is True and type(initial) is int and initial > 1 and
           value['HostConfig']['NetworkMode'] == 'none' and
           value['HostConfig']['Memory'] == value['HostConfig']['MemorySwap'] == ceiling,
           5, 'runtime_container_unproven')
    peer = Path(f'/proc/{pid}/ns/pid').stat()
    owner = Path(f'/proc/{initial}/ns/pid').stat()
    return (peer.st_dev, peer.st_ino) == (owner.st_dev, owner.st_ino)


def _no_resident():
    import json
    for cid in docker('ps', '-q').split():
        rows = json.loads(docker('inspect', '--format', '{{json .HostConfig.DeviceRequests}}', cid))
        a.need(not rows or not any('gpu' in capability for row in rows
               for capability in row.get('Capabilities', [])), 5, 'resident_present')


def run(*, image_path, output_root, model_root, dino_root, decoder_root, seed, pipeline_type):
    approval, approval_sha = a._read(a.APPROVAL_PATH, authority=True)
    binding = a.bindings()
    a.approved(approval, binding)
    provider, _ = a._read(a.PROVIDER_PATH)
    a.need(provider.get('id') == 'trellis2' and provider.get('runnable') is True and
           provider.get('commercial') is True and provider.get('gated') is False and
           provider.get('territory_excluded') == [], 5, 'runtime_provider_unapproved')
    # No non-commercial/gated dependency is available on this geometry path.
    for dependency in provider['licence']['deps']:
        if 'generate' in dependency['used_by']:
            a.need(dependency.get('commercial') is True and dependency.get('gated') is False and
                   dependency.get('territory_excluded') == [], 5, 'runtime_provider_unapproved')
    params = {'seed':seed, 'pipeline_type':pipeline_type}
    a.need(a.calibrated_peak(provider, params)[0] is not None, 3, 'calibration_unavailable')
    image_path = Path(image_path).resolve(strict=True)
    input_sha = core.sha256_file(image_path)
    a.need(type(seed) is int and seed in approval['seeds'] and pipeline_type == approval['pipeline_type'] and
           input_sha == approval['input_sha256'], 5, 'runtime_operation_unapproved')
    image_id = binding['image_id']
    a.need(docker('image', 'inspect', '--format', '{{.Id}}', image_id) == image_id, 4, 'runtime_image_stale')
    root = Path(output_root).absolute()
    a.need(not root.exists() and root.parent.is_dir(), 5, 'output_exists')
    cid = None
    result = {'schema':'af.trellis-native-run/v1', 'rc':3, 'status':'INCOMPLETE',
              'approval_sha256':approval_sha, 'bindings':binding, 'seed':seed, 'pipeline_type':pipeline_type}
    with a.Lease(MODEL_LEASE) as lease:
        _no_resident()
        receipt = scheduler.native_admission(provider, params, lease=lease,
            ceiling_bytes=approval['ceiling_bytes'], min_free_bytes=approval['min_free_bytes'],
            image_id=image_id, provider_path=a.PROVIDER_PATH)
        a.need(receipt['rc'] == 0, receipt['rc'], receipt['reason_codes'][0] if receipt['rc'] else 'admission_invalid')
        root.mkdir(mode=0o700)
        authority_dir = root/'authority'; authority_dir.mkdir(mode=0o700)
        work = root/'work'; work.mkdir(mode=0o700)
        for relative in ('home', 'cache/hf', 'cache/triton', 'cache/cuda'):
            (work/relative).mkdir(parents=True, exist_ok=True)
        name = 'af-g0admit-' + os.urandom(6).hex()
        command = ['create', '--name', name, '--pull=never', '--network=none', '--gpus=all',
                   '--memory='+str(approval['ceiling_bytes']), '--memory-reservation='+str(approval['ceiling_bytes']),
                   '--memory-swap='+str(approval['ceiling_bytes']), '--cpuset-cpus=0-15', '--read-only',
                   '--tmpfs', '/tmp:rw,nosuid,nodev,size=2g', '--user', f'{os.getuid()}:{os.getgid()}']
        mounts = [(FACTORY, '/factory', True), (authority_dir, a.AUTHORITY_DIR, True),
                  (a.APPROVAL_PATH, a.APPROVAL_PATH, True), (image_path, '/input/reference.png', True),
                  (Path(model_root).resolve(strict=True), '/weights/trellis2', True),
                  (Path(dino_root).resolve(strict=True), '/weights/dino', True),
                  (Path(decoder_root).resolve(strict=True), '/weights/decoder', True), (work, '/work', False)]
        for source, target, readonly in mounts:
            a.need(',' not in str(source), 2, 'mount_path_invalid')
            command += ['--mount', f'type=bind,src={source},dst={target}' + (',readonly' if readonly else '')]
        for key, value in {'HOME':'/work/home', 'HF_HOME':'/work/cache/hf', 'HF_HUB_OFFLINE':'1',
                           'HF_HUB_DISABLE_IMPLICIT_TOKEN':'1', 'TRANSFORMERS_OFFLINE':'1',
                           'TRITON_CACHE_DIR':'/work/cache/triton', 'CUDA_CACHE_PATH':'/work/cache/cuda',
                           'PYTHONPATH':'/factory:/opt/trellis2', 'PYTHONDONTWRITEBYTECODE':'1'}.items():
            command += ['--env', key+'='+value]
        command += ['--entrypoint', '/usr/bin/nice', image_id, '-n', '15', 'python3', '-B', '-m',
                    'providers.trellis2.runtime', '--permit', str(a.AUTHORITY_DIR/'permit.json'),
                    '--image', '/input/reference.png', '--out', '/work/generated',
                    '--model-root', '/weights/trellis2', '--dino-root', '/weights/dino',
                    '--decoder-root', '/weights/decoder', '--seed', str(seed), '--pipeline-type', pipeline_type]
        started = time.monotonic_ns()
        lease.arm()
        try:
            cid = docker(*command)
            a.need(len(cid) == 64 and all(c in '0123456789abcdef' for c in cid), 3, 'runtime_container_unproven')
            core.atomic_write_json(root/'container.json', {'container_id':cid, 'name':name, 'create_argv':command})
            # Fresh snapshot immediately before start, still under the same lease.
            receipt = scheduler.native_admission(provider, params, lease=lease,
                ceiling_bytes=approval['ceiling_bytes'], min_free_bytes=approval['min_free_bytes'],
                image_id=image_id, provider_path=a.PROVIDER_PATH)
            a.need(receipt['rc'] == 0, receipt['rc'], receipt['reason_codes'][0] if receipt['rc'] else 'admission_invalid')
            permit = a.make_permit(approval, approval_sha, binding, seed=seed, pipeline_type=pipeline_type,
                                  input_sha256=input_sha, container_id=cid, receipt=receipt)
            core.atomic_write_json(authority_dir/'permit.json', permit)
            (authority_dir/'permit.json').chmod(0o600)
            with a.Authority(authority_dir, permit, lease,
                             lambda pid: _peer_owned(pid, cid, image_id, approval['ceiling_bytes'])):
                docker('start', cid)
                deadline = time.monotonic() + approval['timeout_s']
                while True:
                    state = inspect_container(cid)['State']
                    if not state['Running']: break
                    a.need(time.monotonic() < deadline, 6, 'deadline_exceeded')
                    time.sleep(1)
            child_rc = state['ExitCode']
            result.update(child_rc=child_rc, container_id=cid, scheduler_admission=receipt,
                          native_permit_sha256=core.sha256_json(permit),
                          rc=child_rc if child_rc in core.RC else 5)
            if result['rc'] == 0:
                measurement = core.load_json(work/'generated/runtime-measurement.json', max_bytes=2**20)
                a.need(measurement.get('rc') == 0 and measurement.get('native_permit_sha256') == core.sha256_json(permit)
                       and measurement.get('output_sha256') == core.sha256_file(work/'generated/raw.glb'),
                       4, 'native_measurement_stale')
                result['measurement'] = measurement
        except Refusal as exc:
            result.update(rc=exc.rc, reason_codes=[exc.code])
        finally:
            # No pattern stop, guessed PGID or raw signal. Only this recorded ID.
            if cid is not None and len(cid) == 64:
                state = inspect_container(cid)['State']
                if state['Running']:
                    docker('stop', '--time', '10', cid)
                    state = inspect_container(cid)['State']
                a.need(state['Running'] is False, 3, 'container_release_unproven')
                lease.release_verified()
                result['final_state'] = state
                try:
                    (root/'container.log').write_text(docker('logs', cid))
                except Refusal:
                    result['logs_verdict'] = 'CANNOT_SEE'
            result.update(status=core.STATUS[result['rc']], wall_ms=(time.monotonic_ns()-started)//10**6)
            core.atomic_write_json(root/'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('image', 'out', 'model-root', 'dino-root', 'decoder-root'):
        parser.add_argument('--'+key, type=Path, required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--pipeline-type', required=True, choices=('512', '1024', '1024_cascade', '1536_cascade'))
    try:
        args = parser.parse_args(argv)
        result = run(image_path=args.image, output_root=args.out, model_root=args.model_root,
                     dino_root=args.dino_root, decoder_root=args.decoder_root, seed=args.seed, pipeline_type=args.pipeline_type)
    except Refusal as exc:
        result = {'rc':exc.rc, 'status':core.STATUS[exc.rc], 'reason_codes':[exc.code]}
    except SystemExit as exc:
        if exc.code == 0: return 0
        result = {'rc':2, 'status':'ERROR', 'reason_codes':['usage_invalid']}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(type(exc).__name__+': '+str(exc), file=sys.stderr)
        result = {'rc':3, 'status':'INCOMPLETE', 'reason_codes':['runtime_launch_unproven']}
    print(core.canonical_json(result), end='')
    return result['rc']


if __name__ == '__main__': raise SystemExit(main())
