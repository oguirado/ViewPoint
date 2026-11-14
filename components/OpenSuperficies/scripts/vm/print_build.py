#!/usr/bin/env python3
"""Print an offline image build only after a root-owned per-recipe approval.

--seal is an explicit operator preparation action: hashes all available inputs,
including the static relay and guest disks; it never grants build approval.
"""
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
IMAGE = 'opensuperficies-vm-arm64:0.1.0'
SCHEMA = 'opensuperficies.vm-image-recipe/v1'
ARTIFACTS = ('build/room_CAPSULE/srf-vm-entry', 'build/vm_guest/Image',
             'build/vm_guest/root.raw', 'build/vm_guest/MANIFEST.sha256', 'build/vm_guest/PROVENANCE.json')
SOURCES = ('docker/vm/Dockerfile', 'scripts/vm/print_build.py',
           'scripts/vm/build_vm_image.sh', 'scripts/vm/vendor_fetch.sh',
           'scripts/vm/test_contract.py', 'docs/spec/vm_image.md')

class Refused(Exception):
    pass


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused('duplicate_json_key')
        result[key] = value
    return result


def parse(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise Refused('invalid_json') from None


def base_digest(raw):
    value = raw.strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise Refused('base_digest_unavailable')
    return value


def regular(root, name):
    path = root / name
    # All inputs must resolve within the worktree without symlink components.
    if not path.resolve().is_relative_to(root.resolve()):
        raise Refused('input_outside_worktree')
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise Refused('symlink_input')
    if not path.is_file():
        raise Refused('input_unavailable')
    return path


def digest(root, name):
    h = hashlib.sha256()
    with regular(root, name).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def inputs(root):
    names = set(SOURCES) | set(ARTIFACTS)
    for directory in ('cmd/srf-vm-entry', 'src/vmsurface/launch',
                      'src/vmsurface/proto', 'src/vmsurface/helpers'):
        names.update(str(p.relative_to(root)) for p in (root / directory).rglob('*.go'))
    # The generated helper table must remain bound to its surface sources.
    names.update('src/browserid/surface/' + name for name in
                 ('io.go', 'ax.go', 'downloads.go', 'record.go'))
    names.update(('go.mod', 'go.sum', 'docker/vm/vendor/SHA256SUMS', 'docker/vm/vendor/DIGEST'))
    names.update(str(p.relative_to(root)) for p in (root / 'docker/vm/vendor').glob('*.deb'))
    return names


def vendor(root):
    directory = root / 'docker/vm/vendor'
    value = base_digest(regular(root, 'docker/vm/vendor/DIGEST').read_text())
    raw = regular(root, 'docker/vm/vendor/SHA256SUMS').read_text()
    entries = {}
    for line in raw.splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9.+_~:%-]*\.deb)', line)
        if not match or match[2] in entries:
            raise Refused('invalid_vendor_manifest')
        entries[match[2]] = match[1]
    if not entries or set(entries) != {p.name for p in directory.glob('*.deb')}:
        raise Refused('vendor_manifest_incomplete')
    for name, sha in entries.items():
        if digest(root, 'docker/vm/vendor/' + name) != sha:
            raise Refused('vendor_digest_mismatch')
    dockerfile = regular(root, 'docker/vm/Dockerfile').read_text()
    if dockerfile.splitlines()[1] != 'FROM ubuntu:24.04@' + value:
        raise Refused('dockerfile_base_mismatch')
    return value


def guest(root):
    entries = {}
    for line in regular(root, 'build/vm_guest/MANIFEST.sha256').read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (Image|root\.raw)', line)
        if not match or match[2] in entries:
            raise Refused('guest_manifest_shape')
        entries[match[2]] = match[1]
    if set(entries) != {'Image', 'root.raw'}:
        raise Refused('guest_manifest_incomplete')
    for name, sha in entries.items():
        if digest(root, 'build/vm_guest/' + name) != sha:
            raise Refused('guest_digest_mismatch')
    binary = regular(root, ARTIFACTS[0]).read_bytes()
    # ELF64 little-endian AArch64, statically linked (no PT_INTERP).
    import struct
    if (len(binary) < 64 or binary[:6] != b'\x7fELF\x02\x01'
            or struct.unpack_from('<H', binary, 18)[0] != 183):
        raise Refused('relay_requires_arm64_elf')
    offset = struct.unpack_from('<Q', binary, 32)[0]
    size, count = struct.unpack_from('<HH', binary, 54)
    if size != 56 or count > 256 or offset + size * count > len(binary):
        raise Refused('invalid_elf_headers')
    if any(struct.unpack_from('<I', binary, offset + size * i)[0] == 3 for i in range(count)):
        raise Refused('relay_must_be_static')


def command(root, recipe):
    expected = {'schema', 'image', 'platform', 'approval_path', 'base_image', 'files'}
    if (not isinstance(recipe, dict) or set(recipe) != expected or recipe['schema'] != SCHEMA
            or recipe['image'] != IMAGE or recipe['platform'] != 'linux/arm64'
            or recipe['approval_path'] != APPROVAL):
        raise Refused('recipe_contract_mismatch')
    base = vendor(root)
    if recipe['base_image'] != 'ubuntu:24.04@' + base:
        raise Refused('recipe_base_mismatch')
    if not isinstance(recipe['files'], dict) or set(recipe['files']) != inputs(root):
        raise Refused('recipe_files_mismatch')
    for name, sha in recipe['files'].items():
        if not isinstance(sha, str) or digest(root, name) != sha:
            raise Refused('recipe_file_hash_mismatch')
    guest(root)
    return ['docker', 'build', '--pull=false', '--platform', 'linux/arm64', '--network=none',
            '-f', str(root / 'docker/vm/Dockerfile'), '-t', IMAGE, str(root)]


def verify_base(root, runner=subprocess.run):
    base = vendor(root)
    try:
        result = runner(['docker', 'image', 'inspect', '--format', '{{.Architecture}} {{.Os}}',
                         'ubuntu:24.04@' + base], stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        timeout=15, check=False, text=True)
    except (OSError, subprocess.SubprocessError):
        raise Refused('local_base_unavailable') from None
    if result.returncode != 0 or result.stdout.strip() != 'arm64 linux':
        raise Refused('local_base_unavailable_or_wrong_arch')


def read_approval(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            st = os.fstat(source.fileno())
            if (not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o644
                    or st.st_uid != 0 or st.st_gid != 0):
                raise Refused('approval_requires_root_root_regular_0644')
            raw = source.read(4097)
            if len(raw) > 4096:
                raise Refused('approval_too_large')
            return parse(raw)
    except OSError:
        raise Refused('operator_approval_unavailable') from None


def approved(value, sha):
    if (not isinstance(value, dict) or set(value) != {'schema', 'mode', 'image', 'arch',
            'recipe_sha256', 'approval_id', 'operator_note'} or value['schema'] != 'os.cu.builder-approval/v1'
            or value['mode'] != 'per_build' or value['image'] != IMAGE or value['arch'] != 'aarch64'
            or value['recipe_sha256'] != sha or not isinstance(value['approval_id'], str)
            or not re.fullmatch(r'b2-[0-9]{4}', value['approval_id'])
            or not isinstance(value['operator_note'], str)
            or not re.fullmatch(r'[A-Za-z0-9 .,:_-]{1,200}', value['operator_note'])):
        raise Refused('approval_does_not_match_recipe')


def seal(root):
    base = vendor(root)
    guest(root)
    recipe = dict(schema=SCHEMA, image=IMAGE, platform='linux/arm64', approval_path=APPROVAL,
                  base_image='ubuntu:24.04@' + base,
                  files={name: digest(root, name) for name in sorted(inputs(root))})
    path = root / 'docker/vm/recipe.json'
    # Exclusive temp in the recipe directory, followed by atomic replacement.
    import tempfile
    fd, name = tempfile.mkstemp(prefix='.recipe-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as target:
            json.dump(recipe, target, indent=2)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    try:
        root = Path(__file__).resolve().parents[2]
        if sys.argv[1:] == ['--seal']:
            seal(root)
            return 0
        if sys.argv[1:]:
            return 2
        raw = (root / 'docker/vm/recipe.json').read_bytes()
        recipe_sha = hashlib.sha256(raw).hexdigest()
        approved(read_approval(APPROVAL), recipe_sha)
        argv = command(root, parse(raw))
        verify_base(root)
        print(shlex.join(argv))
        return 0
    except (Refused, OSError, ValueError, TypeError, AttributeError, KeyError):
        print('AUSENTE — VM build inputs or matching operator approval unavailable', file=sys.stderr)
        return 3

if __name__ == '__main__':
    raise SystemExit(main())
