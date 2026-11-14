#!/usr/bin/env python3
"""Fail-closed build-command printer for the Holdings toolchain image. It NEVER builds.

Checks the operator approval (/etc/opensuperficies/builder_approval.json, root:root 0644, bound to
the sha256 of docker/toolchain/recipe.json), the base image id, the Dockerfile hash and every
context artifact hash, then prints the exact `docker build --network=none` argv.
Exit codes: 0 printed, 2 arguments, 3 refused.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys

APPROVAL = '/etc/opensuperficies/builder_approval.json'
IMAGE = 'opensuperficies-toolchain-arm64:0.2.0'
SCHEMA = 'opensuperficies.toolchain-image-recipe/v1'


class Refused(Exception):
    pass


def unique(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise Refused('duplicate_json_key')
        out[k] = v
    return out


def parse(data):
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise Refused('invalid_json') from None


def sha_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def read_approval():
    try:
        fd = os.open(APPROVAL, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o644 or st.st_uid != 0 or st.st_gid != 0:
                raise Refused('approval_requires_root_root_regular_0644')
            data = f.read(4097)
    except OSError:
        raise Refused('operator_approval_unavailable') from None
    if len(data) > 4096:
        raise Refused('approval_too_large')
    return parse(data)


def check(root, approval, raw):
    recipe = parse(raw)
    if (not isinstance(approval, dict) or set(approval) != {'schema', 'mode', 'image', 'arch', 'recipe_sha256', 'approval_id', 'operator_note'}
            or approval['schema'] != 'os.cu.builder-approval/v1' or approval['mode'] != 'per_build'
            or approval['image'] != IMAGE or approval['arch'] != 'aarch64'
            or approval['recipe_sha256'] != hashlib.sha256(raw).hexdigest()):
        raise Refused('approval_does_not_match_recipe')
    if (not isinstance(recipe, dict) or set(recipe) != {'schema', 'image', 'base_image', 'base_image_id', 'dockerfile_sha256', 'context', 'artifacts'}
            or recipe['schema'] != SCHEMA or recipe['image'] != IMAGE):
        raise Refused('recipe_shape')
    dockerfile = root / 'docker/toolchain/Dockerfile'
    if sha_file(dockerfile) != recipe['dockerfile_sha256']:
        raise Refused('dockerfile_hash_mismatch')
    ctx = (root / recipe['context']).resolve()
    if root not in ctx.parents:
        raise Refused('context_outside_repo')
    arts = recipe['artifacts']
    if not isinstance(arts, dict) or not arts or not {'node.tar', 'rust.tar', 'go1.26.10.linux-arm64.tar.gz'} <= set(arts):
        raise Refused('artifacts_shape')
    present = {str(p.relative_to(ctx)) for p in ctx.rglob('*') if p.is_file()}
    if present != set(arts):
        raise Refused('context_files_mismatch')
    for rel, digest in sorted(arts.items()):
        p = ctx / rel
        if p.is_symlink() or sha_file(p) != digest:
            raise Refused('artifact_hash_mismatch')
    got = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', recipe['base_image']],
                         capture_output=True, text=True, timeout=20)
    if got.returncode != 0 or got.stdout.strip() != recipe['base_image_id']:
        raise Refused('base_image_id_mismatch')
    return ['docker', 'build', '--pull=false', '--network=none', '--platform', 'linux/arm64',
            '-t', IMAGE, '-f', str(dockerfile), str(ctx)]


def main(argv):
    if len(argv) != 1:
        print('REFUSED: no arguments accepted', file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[2]
    try:
        raw = (root / 'docker/toolchain/recipe.json').read_bytes()
        print(shlex.join(check(root, read_approval(), raw)))
        return 0
    except (Refused, OSError, KeyError, TypeError, subprocess.SubprocessError) as e:
        print('REFUSED: ' + (str(e) if isinstance(e, Refused) else 'recipe_unavailable'), file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main(sys.argv))
