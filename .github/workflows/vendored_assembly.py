"""Verify shipped engine trees before running the existing assembly smokes."""
import argparse
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / 'agents'), str(ROOT / 'tools')]

import assembly_smoke
from metaviewer_agents.perception import deps_lock


def verify(root=ROOT):
    """Require vendored mode; retain the dependency lock's content checks."""
    root = Path(root).resolve()
    lock = root / 'deps.lock'
    pins = assembly_smoke.parse_deps_lock(lock)
    entries = deps_lock._locked_entries(lock)
    paths = {}
    expected_paths = {'vision-engine': 'components/vision-engine',
                      'wender-agent': 'components/wender-agent',
                      'opensuperficies': 'components/OpenSuperficies'}
    for name in assembly_smoke.REQUIRED_COMPONENTS:
        entry = entries[name]
        if (entry.get('vendored_path') != expected_paths[name]
                or not re.fullmatch(r'[0-9a-f]{64}', entry.get('vendored_tree_sha256', ''))):
            raise ValueError(name + ': vendored lock identity missing or invalid')
        path = root / expected_paths[name]
        if path.is_symlink() or not path.is_dir() or (path / '.git').exists():
            raise ValueError(name + ': plain vendored directory required')
        # OpenSuperficies has no public resolver; use the same digest verifier.
        deps_lock._verify_vendored_tree(path, entry['vendored_tree_sha256'], name)
        if name in deps_lock._ROOT_VARIABLES:
            variable = deps_lock._ROOT_VARIABLES[name]
            override = {'wender-agent': 'MV_WENDER_EXECUTABLE',
                        'vision-engine': 'MV_VISION_EXECUTABLE'}[name]
            if override in os.environ:
                raise ValueError(name + ': compiled override is not vendored mode')
            if variable in os.environ and Path(os.environ[variable]).resolve() != path:
                raise ValueError(name + ': external root is not vendored mode')
            if deps_lock.resolve_locked_component(name, lock_path=lock) != path:
                raise ValueError(name + ': unexpected dependency root')
        paths[name] = path
        print('PASS ' + name + ': vendored_tree_sha256=' + entry['vendored_tree_sha256'])
    return pins, paths


def smoke(pins, paths, timeout):
    """Run every original probe, including the required Wender planner."""
    with tempfile.TemporaryDirectory(prefix='vp-vendored-smoke-') as temporary:
        # The isolated vision subprocess ignores PYTHONDONTWRITEBYTECODE.
        # Keep its caches and all Go build side effects out of locked inputs.
        snapshot = Path(temporary) / 'checkout'
        snapshot.mkdir()
        shutil.copy2(ROOT / 'deps.lock', snapshot / 'deps.lock')
        for directory in ('agents', 'daemon-adapter', 'contracts'):
            shutil.copytree(ROOT / directory, snapshot / directory, symlinks=True,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
        for name, source in paths.items():
            destination = snapshot / source.relative_to(ROOT)
            shutil.copytree(source, destination, symlinks=True)
        _, copied = verify(snapshot)
        previous_root = assembly_smoke.REPO_ROOT
        try:
            assembly_smoke.REPO_ROOT = snapshot
            results = [
                assembly_smoke.smoke_vision_engine(
                    copied['vision-engine'], pins['vision-engine'], timeout_s=timeout),
                assembly_smoke.smoke_wender_agent(
                    copied['wender-agent'], pins['wender-agent'],
                    timeout_s=timeout, require_planner=True),
                assembly_smoke.smoke_opensuperficies(
                    copied['opensuperficies'], pins['opensuperficies'],
                    scratch_root=Path(temporary), timeout_s=timeout),
            ]
        finally:
            assembly_smoke.REPO_ROOT = previous_root
    ok, lines = assembly_smoke.aggregate_results(results)
    for line in lines:
        print(line)
    # Probes must not contaminate the vendored inputs for later consumers.
    verify(ROOT)
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('timeout must be positive')
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    try:
        pins, paths = verify()
        return smoke(pins, paths, args.timeout) if args.smoke else 0
    except (ValueError, OSError, deps_lock.DependencyLockError) as error:
        print('FAIL vendored assembly: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
