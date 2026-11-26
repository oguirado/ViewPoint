"""Assemble the ARM64 two-lane image from local, digest-pinned bytes."""

import argparse
import hashlib
import json
from pathlib import Path
import stat
import struct
import subprocess
import tempfile

from .image import APPLETS, ImageError, encode_initramfs, static_arm64


GUEST = Path(__file__).with_name('lanes_guest')
LIBS = ('ld-linux-aarch64.so.1', *(f'aarch64-linux-gnu/{name}' for name in (
    'libc.so.6', 'libcap.so.2', 'libpcre2-8.so.0', 'libselinux.so.1', 'libz.so.1',
)))
SYSTEM = {
    'busybox': 'usr/bin/busybox',
    'bwrap': 'usr/bin/bwrap',
    'git': 'usr/bin/git',
    **{f'lib/{name}': f'lib/{name}' for name in LIBS},
}
LTP = {
    'openat201': 'openat2',
    'openat203': 'openat2',
    'ptrace01': 'ptrace',
    'unshare02': 'unshare',
    'mount03': 'mount',
    'mount03_suid_child': 'mount',
}
SCRIPTS = {'init.sh': 'init', 'run.sh': 'sbin/lane-run', 'smoke.sh': 'sbin/lane-smoke'}
DEVICES = (
    ('console', 0o600, 5, 1), ('null', 0o666, 1, 3), ('zero', 0o666, 1, 5),
    ('full', 0o666, 1, 7), ('random', 0o666, 1, 8), ('urandom', 0o666, 1, 9),
    ('tty', 0o666, 5, 0),
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_pin(path, expected, limit=16 << 20):
    with path.open('rb') as source:
        data = source.read(limit + 1)
    if not data or len(data) > limit or digest(data) != expected:
        raise ImageError(f'digest or size mismatch: {path}')
    return data


def build(input_root, ltp_root, lock):
    pins = lock['inputs']
    ltp_paths = {f'ltp/{name}': ltp_root / 'testcases/kernel/syscalls' / category / name
                 for name, category in LTP.items()}
    if set(pins) != {*SYSTEM, *ltp_paths, *SCRIPTS, 'enter.c'}:
        raise ImageError('lock must pin every allowlisted input exactly once')
    assets = {name: read_pin(input_root / path, pins[name]) for name, path in SYSTEM.items()}
    ltp = {name: read_pin(path, pins[name]) for name, path in ltp_paths.items()}
    for executable in ltp.values():
        static_arm64(executable, 4 << 20)
    static_arm64(assets['busybox'], 4 << 20)
    for name in ('bwrap', 'git', *(f'lib/{lib}' for lib in LIBS)):
        data = assets[name]
        if (len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01'
                or struct.unpack_from('<H', data, 18)[0] != 183):
            raise ImageError(f'ARM64 ELF required: {name}')
    scripts = {target: read_pin(GUEST / source, pins[source])
               for source, target in SCRIPTS.items()}
    with tempfile.TemporaryDirectory(prefix='atalaya-lane-build-') as directory:
        work = Path(directory)
        (work / 'enter.c').write_bytes(read_pin(GUEST / 'enter.c', pins['enter.c']))
        compiler = Path('/usr/bin/cc').resolve(strict=True)
        read_pin(compiler, lock['compiler_sha256'], 128 << 20)
        subprocess.run(
            [str(compiler), '-O2', '-Wall', '-Wextra', '-Werror',
             '-static', '-s', '-Wl,--build-id=none', '-o', 'lane-enter', 'enter.c'],
            cwd=work, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SOURCE_DATE_EPOCH': '0'},
            check=True,
        )
        enter = (work / 'lane-enter').read_bytes()
    static_arm64(enter, 4 << 20)
    if digest(enter) != lock['lane_enter_sha256']:
        raise ImageError('compiled lane-enter digest mismatch')

    def entry(path, kind, mode, data=b''):
        return path, kind | mode, data, 0, 0

    entries = [entry(name, stat.S_IFDIR, 0o700 if name in ('root', 'run/atalaya') else 0o755)
               for name in ('.', 'bin', 'dev', 'proc', 'sys', 'sys/fs', 'sys/fs/cgroup', 'work',
                            'etc', 'etc/git-template', 'run', 'run/atalaya', 'root', 'sbin', 'sbin/ltp', 'lib',
                            'lib/aarch64-linux-gnu', 'objects')]
    entries += [entry(path, stat.S_IFREG, 0o500, data) for path, data in scripts.items()]
    entries += [entry('sbin/' + name, stat.S_IFREG, 0o500, data) for name, data in ltp.items()]
    entries += [entry('tmp', stat.S_IFDIR, 0o1777),
                entry('bin/lane-enter', stat.S_IFREG, 0o555, enter),
                entry('etc/passwd', stat.S_IFREG, 0o444,
                      b'root:x:0:0:root:/root:/bin/sh\nlane0:x:10000:10000::/work:/bin/sh\n'
                      b'lane1:x:10001:10001::/work:/bin/sh\nnobody:x:65534:65534::/:/bin/false\n'),
                entry('etc/group', stat.S_IFREG, 0o444,
                      b'root:x:0:\nlane0:x:10000:\nlane1:x:10001:\nnogroup:x:65534:\n')]
    entries += [entry(f'bin/{name}', stat.S_IFLNK, 0o777, b'busybox') for name in APPLETS]
    entries += [entry(name if name.startswith('lib/') else 'bin/' + name,
                      stat.S_IFREG, 0o444 if name.startswith('lib/aarch64-linux-gnu/') else 0o555, data)
                for name, data in assets.items()]
    # Bubblewrap binds these from the outer /dev before mounting private devpts.
    entries += [(f'dev/{name}', stat.S_IFCHR | mode, b'', major, minor)
                for name, mode, major, minor in DEVICES]
    entries.append(entry('TRAILER!!!', 0, 0))
    if len({row[0] for row in entries}) != len(entries) or any(row[1] & 0o6000 for row in entries):
        raise ImageError('duplicate or set-ID archive entry')
    data = encode_initramfs(entries)
    if digest(data) != lock['image_sha256']:
        raise ImageError('image digest mismatch')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-root', required=True, type=Path)
    parser.add_argument('--ltp-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    options = parser.parse_args()
    lock = json.loads((GUEST / 'inputs.json').read_bytes())
    data = build(options.input_root, options.ltp_root, lock)
    with options.output.open('xb') as destination:
        destination.write(data)
    print(digest(data), options.output)


if __name__ == '__main__':
    main()
