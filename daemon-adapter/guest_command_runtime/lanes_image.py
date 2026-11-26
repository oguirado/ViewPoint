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
LTP_SUBSET = ('openat201', 'openat203', 'ptrace01', 'unshare02', 'mount03')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_pin(path, expected, limit=16 << 20):
    with path.open('rb') as source:
        data = source.read(limit + 1)
    if not data or len(data) > limit or digest(data) != expected:
        raise ImageError(f'digest or size mismatch: {path}')
    return data


def build(input_root, broker, lock):
    pins = lock['inputs']
    if set(pins) != {*SYSTEM, 'broker', 'enter.c', 'run.sh'}:
        raise ImageError('lock must pin every allowlisted input exactly once')
    assets = {name: read_pin(input_root / path, pins[name]) for name, path in SYSTEM.items()}
    init = read_pin(broker, pins['broker'])
    static_arm64(init, 12 << 20)
    static_arm64(assets['busybox'], 4 << 20)
    for name in ('bwrap', 'git', *(f'lib/{lib}' for lib in LIBS)):
        data = assets[name]
        if (len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01'
                or struct.unpack_from('<H', data, 18)[0] != 183):
            raise ImageError(f'ARM64 ELF required: {name}')
    runner = read_pin(GUEST / 'run.sh', pins['run.sh'])
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
                            'etc', 'etc/git-template', 'run', 'run/atalaya', 'root', 'sbin', 'lib',
                            'lib/aarch64-linux-gnu', 'objects')]
    entries += [entry('init', stat.S_IFREG, 0o500, init),
                entry('etc/empty-cmdline', stat.S_IFREG, 0o400),
                entry('tmp', stat.S_IFLNK, 0o777, b'/work/tmp'),
                entry('sbin/lane-run', stat.S_IFREG, 0o500, runner),
                entry('bin/lane-enter', stat.S_IFREG, 0o555, enter),
                entry('etc/passwd', stat.S_IFREG, 0o444,
                      b'root:x:0:0:root:/root:/bin/sh\nlane0:x:10000:10000::/work:/bin/sh\n'
                      b'lane1:x:10001:10001::/work:/bin/sh\n'),
                entry('etc/group', stat.S_IFREG, 0o444, b'root:x:0:\nlane0:x:10000:\nlane1:x:10001:\n'),
                entry('etc/ltp-subset', stat.S_IFREG, 0o444, ('\n'.join(LTP_SUBSET) + '\n').encode())]
    entries += [entry(f'bin/{name}', stat.S_IFLNK, 0o777, b'busybox') for name in APPLETS]
    entries += [entry(name if name.startswith('lib/') else 'bin/' + name,
                      stat.S_IFREG, 0o444 if name.startswith('lib/') else 0o555, data)
                for name, data in assets.items()]
    # Kernel console and v1 bootstrap device metadata stay compatible.
    entries += [(f'dev/{name}', stat.S_IFCHR | mode, b'', major, minor)
                for name, mode, major, minor in (
                    ('console', 0o600, 5, 1), ('null', 0o666, 1, 3),
                    ('zero', 0o666, 1, 5), ('urandom', 0o666, 1, 9))]
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
    parser.add_argument('--broker', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    options = parser.parse_args()
    lock = json.loads((GUEST / 'inputs.json').read_bytes())
    data = build(options.input_root, options.broker, lock)
    with options.output.open('xb') as destination:
        destination.write(data)
    print(digest(data), options.output)


if __name__ == '__main__':
    main()
