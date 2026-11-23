"""Shadow/on vision stage using only the configured hash-pinned wheel.

Engine imports run in a fresh interpreter so a caller's previously imported
visioncore cannot replace the pinned adapter. No engine source is modified.
STORAGE LAW v1: the adapter copy (vision_in) and the tile buffers (tiles.bin/gops.bin) are written into a
vadapt.ramws RAM workspace; only the digest-bound evidence (vision.json, *.jsonl, manifests) is copied back to
<run>/vision, then the workspace is released.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]

if not __package__:
    # Direct worker execution under -I loads the component beside this script.
    __package__ = "blender_visual_qa"
    spec = importlib.util.spec_from_file_location(__package__, Path(__file__).resolve().with_name("__init__.py"))
    package = importlib.util.module_from_spec(spec)
    sys.modules[__package__] = package
    spec.loader.exec_module(package)

from .vision_delivery import admit_engine_imports, resolve_wheel


class StageError(ValueError):
    pass


def pinned_wheel(config=None, expected_commit=None):
    try:
        wheel, pin = resolve_wheel(config, relative_root=ROOT, expected_commit=expected_commit)
        with zipfile.ZipFile(wheel) as archive:
            archive.getinfo('vadapt/blender/from_render_dir.py')
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        raise StageError(f'E_PIN: {exc}') from exc
    return wheel.resolve(), pin


def run_stage(run_dir, *, vision='shadow', config=None, expected_commit=None, timeout=120):
    if vision not in ('off', 'shadow', 'on'):
        raise StageError('E_SCHEMA: vision mode')
    if vision == 'off':
        return {'mode': 'off', 'status': 'SKIPPED'}
    config = Path(config).resolve() if config else None
    wheel, pin = pinned_wheel(config, expected_commit)
    python_flags = '-IB' if sys.flags.isolated else '-B'
    cmd = [sys.executable, python_flags, str(Path(__file__).resolve()), '--worker', '--run', str(Path(run_dir).resolve()),
           '--vision', vision, '--expected-commit', pin['commit'], '--admitted-wheel', str(wheel)]
    if config is not None:
        cmd += ['--config', str(config)]
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
    if (result.pop('engine_wheel', None), result.get('engine_commit'), result.get('wheel_sha256')) != (
            str(wheel), pin['commit'], pin['wheel_sha256']):
        raise StageError('E_PIN: worker receipt')
    return result


def _worker(run_dir, config, mode, *, expected_commit, admitted_wheel):
    wheel, pin = pinned_wheel(config, expected_commit)
    if str(wheel) != admitted_wheel:
        raise StageError('E_PIN: worker selected a different wheel')
    try:
        finder = admit_engine_imports(wheel)
        from vadapt.blender import from_render_dir
        from visioncore import pipeline, params
        from vadapt import ramws
        for module in (from_render_dir, pipeline, params, ramws):
            finder.check_origin(module)
    except (ImportError, ValueError) as exc:
        raise StageError(f'E_PIN: {exc}') from exc
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
            'engine_commit': pin['commit'], 'wheel_sha256': pin['wheel_sha256'], 'engine_wheel': str(wheel),
            'proposals': []}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True)
    ap.add_argument('--vision', choices=('off', 'shadow', 'on'), default='shadow')
    ap.add_argument('--config')
    ap.add_argument('--expected-commit')
    ap.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--admitted-wheel', help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        result = (_worker(a.run, a.config, a.vision, expected_commit=a.expected_commit,
                          admitted_wheel=a.admitted_wheel) if a.worker else
                  run_stage(a.run, vision=a.vision, config=a.config, expected_commit=a.expected_commit))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(json.dumps({'code': 'E_VISION', 'error': str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
