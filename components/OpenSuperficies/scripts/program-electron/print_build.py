#!/usr/bin/env python3
"""Fail-closed build-command printer; only reads Docker local image metadata."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

APPROVAL = '/etc/opensuperficies/builder_approval.json'
IMAGE = 'opensuperficies-program-electron-arm64:0.1.0'
BASE = 'opensuperficies-desktop-arm64:1.1.0'
BASE_ID = 'sha256:a15b2c19d65f01f3f2f482fa62a06e1fee41bbd6e4af741892754a34d3b68382'
PACKAGE_PINS = {'python3-gi': '3.42.2-3+b1', 'gir1.2-gtk-3.0': '3.24.38-2~deb12u3', 'libatk-bridge2.0-0': '2.46.0-5', 'at-spi2-core': '2.46.0-5', 'xdotool': '1:3.20160805.1-5', 'xpra': '3.1.3-0.1'}


class Refused(Exception):
    pass


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused('duplicate_json_key')
        result[key] = value
    return result


def parse(data):
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise Refused('invalid_json') from None


def read_approval(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            st = os.fstat(source.fileno())
            if (not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o644
                    or st.st_uid != 0 or st.st_gid != 0):
                raise Refused('approval_requires_root_root_regular_0644')
            data = source.read(4097)
            if len(data) > 4096:
                raise Refused('approval_too_large')
            return parse(data)
    except OSError:
        raise Refused('operator_approval_unavailable') from None


def approved(value, recipe_sha):
    if not isinstance(value, dict) or set(value) != {
            'schema', 'mode', 'image', 'arch', 'recipe_sha256', 'approval_id', 'operator_note'}:
        raise Refused('approval_shape')
    if (value['schema'] != 'os.cu.builder-approval/v1' or value['mode'] != 'per_build'
            or value['image'] != IMAGE or value['arch'] != 'aarch64'
            or value['recipe_sha256'] != recipe_sha
            or not isinstance(value['approval_id'], str)
            or not re.fullmatch(r'b2-[0-9]{4}', value['approval_id'])
            or not isinstance(value['operator_note'], str)
            or not re.fullmatch(r'[A-Za-z0-9 .,:_-]{1,200}', value['operator_note'])):
        raise Refused('approval_does_not_match_recipe')


def command(root, recipe):
    if recipe.get('base_image_id') != BASE_ID:
        raise Refused('base_image_id_mismatch')
    if recipe.get('package_pins') != PACKAGE_PINS:
        raise Refused('package_version_mismatch')
    if (recipe.get('schema') != 'opensuperficies.program-electron-recipe/v1'
            or recipe.get('platform') != 'linux/arm64'
            or recipe.get('approval_path') != APPROVAL):
        raise Refused('recipe_contract_mismatch')
    if recipe.get('base_image') != BASE or recipe.get('image') != IMAGE:
        raise Refused('recipe_image_mismatch')
    files = recipe.get('files')
    expected = {'docker/program-electron/Dockerfile', 'docker/program/entrypoint.sh',
                'docker/identity/xpra-layer.sh', 'docker/program-electron/app.json',
                'docker/program-electron/README.md', 'scripts/program-electron/pin_app.py',
                'scripts/program-electron/print_build.py', 'scripts/program-electron/build_electron_image.sh',
                'scripts/program-electron/test_contract.py', 'scripts/program-electron/test_image.py',
                'docs/spec/program_electron.md'}
    if not isinstance(files, dict) or set(files) != expected:
        raise Refused('recipe_files_mismatch')
    for name, sha in files.items():
        path = root / name
        if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != sha):
            raise Refused('recipe_file_hash_mismatch')
    verify_app(root, recipe)
    return ['docker', 'build', '--pull=false', '--platform', 'linux/arm64', '--network=none',
            '-f', str(root / 'docker/program-electron/Dockerfile'), '-t', IMAGE, str(root)]


def verify_app(root, recipe):
    manifest_path = root / 'docker/program-electron/app.manifest'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise Refused('app_manifest_unavailable')
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != recipe.get('app_manifest_sha256'):
        raise Refused('app_manifest_hash_mismatch')
    if not raw:
        raise Refused('AUSENTE — app_manifest_empty')
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        raise Refused('app_manifest_invalid') from None
    if not text.endswith('\n'):
        raise Refused('app_manifest_invalid')
    expected = {}
    for line in text.splitlines():
        sha, separator, name = line.partition('  ')
        if (not separator or not re.fullmatch(r'[0-9a-f]{64}', sha)
                or not name or name.startswith('/') or '\\' in name
                or any(part in ('', '.', '..') for part in name.split('/'))
                or name in expected):
            raise Refused('app_manifest_invalid')
        expected[name] = sha
    if list(expected) != sorted(expected):
        raise Refused('app_manifest_not_sorted')
    stage = root / 'build/electron_app'
    if stage.is_symlink() or not stage.is_dir():
        raise Refused('AUSENTE — staged_app_missing')
    actual = set()
    # No symlinks remain after pinning, and no device is opened for hashing.
    for directory, dirs, names in os.walk(stage, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                if stat.S_IMODE(info.st_mode) != 0o555:
                    raise Refused('app_directory_mode')
                continue
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o444, 0o555):
                raise Refused('app_file_type_or_mode')
            relative = path.relative_to(stage).as_posix()
            if expected.get(relative) != hashlib.sha256(path.read_bytes()).hexdigest():
                raise Refused('app_file_hash_mismatch')
            actual.add(relative)
    if actual != set(expected):
        raise Refused('app_file_set_mismatch')
    app = parse((root / 'docker/program-electron/app.json').read_bytes())
    if not isinstance(app, dict) or not isinstance(app.get('exe'), str) or app['exe'] not in expected:
        raise Refused('app_executable_not_pinned')
    if not os.access(stage / app['exe'], os.X_OK):
        raise Refused('app_executable_not_executable')


def verify_base(runner=subprocess.run):
    try:
        result = runner(['docker', 'image', 'inspect', '--format', '{{.Id}}', BASE],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, timeout=15, check=False, text=True)
    except (OSError, subprocess.SubprocessError):
        raise Refused('local_base_unavailable') from None
    if result.returncode != 0 or result.stdout.strip() != BASE_ID:
        raise Refused('local_base_image_id_mismatch')


def _print_build(approval_path, runner=subprocess.run):
    try:
        approval = read_approval(approval_path)
        root = Path(__file__).resolve().parents[2]
        raw = (root / 'docker/program-electron/recipe.json').read_bytes()
        recipe_sha = hashlib.sha256(raw).hexdigest()
        approved(approval, recipe_sha)
        argv = command(root, parse(raw))
        verify_base(runner)
        print(shlex.join(argv))
        return 0
    except (Refused, OSError, TypeError, AttributeError) as exc:
        # Never print JSON contents, profile data, or arbitrary error paths.
        detail = str(exc) if isinstance(exc, Refused) else 'recipe_unavailable'
        print('REFUSED: ' + detail, file=sys.stderr)
        return 3


def main():
    return _print_build(APPROVAL)


if __name__ == '__main__':
    sys.exit(main())
