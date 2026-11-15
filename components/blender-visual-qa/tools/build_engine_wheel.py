#!/usr/bin/env python3
"""Build the declared engine commit in SB0 scratch; never build in its source checkout."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    if list(sys.argv[1:] if argv is None else argv):
        print(json.dumps({"code":"E_USAGE","rc":2,"reason":"no_arguments_supported"},sort_keys=True,separators=(",", ":")))
        return 2
    vendor = json.loads((ROOT / 'tools/VENDORED.json').read_text())
    base = ROOT / 'build/room_SB0'
    tree = base / ('engine-' + vendor['commit'])
    wheels = base / 'wheels'
    tree.mkdir(parents=True, exist_ok=True)
    wheels.mkdir(parents=True, exist_ok=True)
    archive = subprocess.check_output(['git', '-C', vendor['src_repo'], 'archive', vendor['commit']], timeout=30)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        # Only regular files/directories under the committed tree are admitted.
        for item in tar.getmembers():
            if not (item.isfile() or item.isdir()) or not (tree / item.name).resolve().is_relative_to(tree.resolve()):
                raise ValueError('unsafe archive member')
        tar.extractall(tree, filter='data')
    metadata = tree / 'pyproject.toml'
    original = metadata.read_text()
    before = 'packages = ["visioncore", "vmem", "vadapt"]'
    after = 'packages = ["visioncore", "vmem", "vadapt", "vadapt.blender"]'
    if original.count(before) != 1:
        raise ValueError('unrecognized engine package manifest')
    metadata.write_text(original.replace(before, after))
    result = subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--no-cache-dir', str(tree), '--wheel-dir', str(wheels)], capture_output=True, timeout=120)
    (base / 'wheel-build.log').write_bytes(result.stdout + result.stderr)
    if result.returncode:
        print(json.dumps({'code': 'E_IO', 'rc': 2, 'reason': 'wheel_build', 'log': 'build/room_SB0/wheel-build.log'}, sort_keys=True))
        return 2
    files = sorted(wheels.glob('vision_engine-1.1.0-*.whl'))
    if len(files) != 1:
        print(json.dumps({'code': 'E_IO', 'rc': 2, 'reason': 'wheel_inventory'}, sort_keys=True))
        return 2
    wheel = files[0]
    config = {'mode': 'shadow', 'engine_path': wheel.relative_to(ROOT).as_posix(), 'wheel_build': {'metadata_patch': 'add vadapt.blender to setuptools package list; engine Python source unchanged'}, 'pin': {'version': '1.1.0', 'commit': vendor['commit'], 'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}}
    target = ROOT / 'config/vision.json'
    temp = target.with_suffix('.json.tmp')
    temp.write_text(json.dumps(config, sort_keys=True, separators=(',', ':')) + '\n')
    temp.replace(target)
    print(json.dumps({'code': 'OK', 'rc': 0, 'wheel_sha256': config['pin']['wheel_sha256']}, sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    try:
        rc = main()
    except subprocess.TimeoutExpired:
        print(json.dumps({'code':'E_BUDGET','rc':6,'reason':'timeout'},sort_keys=True,separators=(',', ':')))
        rc = 6
    except (OSError,ValueError,KeyError,subprocess.SubprocessError,tarfile.TarError) as exc:
        print(json.dumps({'code':'E_IO','rc':2,'reason':str(exc)},sort_keys=True,separators=(',', ':')))
        rc = 2
    sys.exit(rc)
