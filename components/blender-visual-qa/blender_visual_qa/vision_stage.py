"""Shadow/on vision stage using only the configured hash-pinned wheel.

Engine imports run in a fresh interpreter so a caller's previously imported
visioncore cannot replace the pinned adapter. No engine source is modified.
STORAGE LAW v1: the adapter copy (vision_in) and the tile buffers (tiles.bin/gops.bin) are written into a
vadapt.ramws RAM workspace; only the digest-bound evidence (vision.json, *.jsonl, manifests) is copied back to
<run>/vision, then the workspace is released.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class StageError(ValueError):
    pass


def pinned_wheel(config, expected_commit=None):
    config = Path(config)
    cfg = json.loads(config.read_text())
    pin = cfg['pin']
    if expected_commit is not None and pin['commit'] != expected_commit:
        raise StageError('E_PIN: configured engine commit differs from required commit')
    wheel = Path(cfg['engine_path'])
    if not wheel.is_absolute():
        wheel = ROOT / wheel
    if not wheel.is_file() or hashlib.sha256(wheel.read_bytes()).hexdigest() != pin['wheel_sha256']:
        raise StageError('E_PIN: wheel sha256')
    with zipfile.ZipFile(wheel) as archive:
        if 'vadapt/blender/from_render_dir.py' not in archive.namelist():
            raise StageError('E_PIN: vadapt.blender AUSENTE')
    return wheel.resolve(), pin


def run_stage(run_dir, *, vision='shadow', config=None, expected_commit=None, timeout=120):
    if vision not in ('off', 'shadow', 'on'):
        raise StageError('E_SCHEMA: vision mode')
    if vision == 'off':
        return {'mode': 'off', 'status': 'SKIPPED'}
    config = Path(config or ROOT / 'config/vision.json').resolve()
    wheel, pin = pinned_wheel(config, expected_commit)
    cmd = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker', '--run', str(Path(run_dir).resolve()),
           '--config', str(config), '--vision', vision]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    # Only the wheel provides engine modules; never inherit a caller's source path.
    env.pop('PYTHONPATH', None)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise StageError('E_TIMEOUT: vision stage') from exc
    except BaseException:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    if proc.returncode:
        raise StageError('E_VISION: ' + stderr.decode(errors='replace')[-2000:] + stdout.decode(errors='replace')[-2000:])
    result = json.loads(stdout)
    if result.get('wheel_sha256') != pin['wheel_sha256']:
        raise StageError('E_PIN: worker receipt')
    return result


def _worker(run_dir, config, mode):
    wheel, pin = pinned_wheel(config)
    sys.path.insert(0, str(wheel))
    from vadapt.blender import from_render_dir
    from visioncore import pipeline, params
    for module in (from_render_dir, pipeline):
        if not str(module.__file__).startswith(str(wheel) + '/'):
            raise StageError('E_PIN: module origin')
    from vadapt import ramws
    run_dir = Path(run_dir)
    ws = ramws.open_run('vision_stage', 'vs-%d' % os.getpid(), tool='bvqa')
    try:
        vin = ws.path('vision_in/')
        from_render_dir.adapt(run_dir / 'render', vin)
        tmp = ws.path('vision/')
        pipeline.analyze(pipeline.ManifestSource(vin / 'frames.manifest.json'), params.effective(None), None, tmp)
        ws.account()
        dst = run_dir / 'vision'
        dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(tmp.rglob('*')):  # L6: evidence only, never a frame or tile buffer
            if f.is_file() and not f.name.lower().endswith(ramws.FRAME_EXT) and f.name not in ramws.FRAME_NAMES:
                t = dst / f.relative_to(tmp)
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_bytes(f.read_bytes())
        receipt = ws.receipt()
    finally:
        ws.close()
    return {'ram_peak_bytes': receipt['ram_peak_bytes'], 'mode': mode, 'advisory': mode == 'shadow', 'status': 'COMPLETE',
            'engine_commit': pin['commit'], 'wheel_sha256': pin['wheel_sha256'],
            'proposals': []}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True)
    ap.add_argument('--vision', choices=('off', 'shadow', 'on'), default='shadow')
    ap.add_argument('--config', default=str(ROOT / 'config/vision.json'))
    ap.add_argument('--expected-commit')
    ap.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        result = (_worker(a.run, a.config, a.vision) if a.worker else
                  run_stage(a.run, vision=a.vision, config=a.config, expected_commit=a.expected_commit))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(json.dumps({'code': 'E_VISION', 'error': str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
