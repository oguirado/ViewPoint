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
IMAGE = 'opensuperficies-identity-browser-arm64:0.1.0'
BASE = 'opensuperficies-desktop-arm64:1.1.0'
BASE_ID = 'sha256:a15b2c19d65f01f3f2f482fa62a06e1fee41bbd6e4af741892754a34d3b68382'
CHROMIUM = '152.0.7977.82-1~deb12u1'


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


def read_approval():
    try:
        fd = os.open(APPROVAL, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
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
    if recipe.get('chromium_debian_version') != CHROMIUM:
        raise Refused('chromium_version_mismatch')
    if recipe.get('base_image') != BASE or recipe.get('image') != IMAGE:
        raise Refused('recipe_image_mismatch')
    files = recipe.get('files')
    expected = {'docker/identity/Dockerfile', 'docker/identity/entrypoint.sh',
                'docker/identity/xpra-layer.sh', 'docker/identity/ax-shim.py',
                'docker/identity/ax-allowlist.json', 'scripts/identity/test_ax_shim.py',
                'docs/spec/browserid_ax.md', 'scripts/identity/test_ax_review.py',
                'scripts/identity/build_identity_image.sh', 'scripts/identity/print_build.py',
                'scripts/identity/test_contract.py', 'scripts/identity/test_image.py',
                'docs/spec/browserid_image.md'}
    if not isinstance(files, dict) or set(files) != expected:
        raise Refused('recipe_files_mismatch')
    for name, sha in files.items():
        path = root / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise Refused('recipe_file_hash_mismatch')
    return ['docker', 'build', '--pull=false', '--platform', 'linux/arm64', '--network=none',
            '-f', str(root / 'docker/identity/Dockerfile'), '-t', IMAGE, str(root)]


def verify_base(runner=subprocess.run):
    try:
        result = runner(['docker', 'image', 'inspect', '--format', '{{.Id}}', BASE],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, timeout=15, check=False, text=True)
    except (OSError, subprocess.SubprocessError):
        raise Refused('local_base_unavailable') from None
    if result.returncode != 0 or result.stdout.strip() != BASE_ID:
        raise Refused('local_base_image_id_mismatch')


def main():
    try:
        approval = read_approval()
        root = Path(__file__).resolve().parents[2]
        raw = (root / 'docker/identity/recipe.json').read_bytes()
        recipe_sha = hashlib.sha256(raw).hexdigest()
        approved(approval, recipe_sha)
        argv = command(root, parse(raw))
        verify_base()
        print(shlex.join(argv))
        return 0
    except (Refused, OSError, TypeError, AttributeError) as exc:
        # Never print JSON contents, profile data, or arbitrary error paths.
        detail = str(exc) if isinstance(exc, Refused) else 'recipe_unavailable'
        print('REFUSED: ' + detail, file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
