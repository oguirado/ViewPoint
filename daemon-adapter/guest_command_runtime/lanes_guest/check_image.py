"""Regression controls for the offline image entry and bubblewrap prerequisites."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
from shutil import copyfile
import stat
import subprocess
import tempfile

from guest_command_runtime import kernel_gate
from guest_command_runtime.image import encode_initramfs, static_arm64
from guest_command_runtime.lanes_image import GUEST
from guest_command_runtime.test_image import unpack


def check_entry(entries):
    for path, source in (('init', 'init.sh'), ('sbin/lane-smoke', 'smoke.sh'),
                         ('sbin/lane-run', 'run.sh')):
        fields, payload = entries[path]
        assert fields[1] == stat.S_IFREG | 0o500, path
        assert payload == (GUEST / source).read_bytes(), path
    pins = json.loads((GUEST / 'inputs.json').read_bytes())['inputs']
    for name in ('openat201', 'openat203', 'ptrace01', 'unshare02', 'mount03', 'mount03_suid_child'):
        fields, payload = entries['sbin/ltp/' + name]
        assert fields[1] == stat.S_IFREG | 0o500, name
        static_arm64(payload, 4 << 20)
        assert sha256(payload).hexdigest() == pins['ltp/' + name], name
    assert all(fields[1] & (stat.S_ISUID | stat.S_ISGID) == 0
               for fields, _ in entries.values())


def check_devices(entries):
    for name, major, minor in (('null', 1, 3), ('zero', 1, 5), ('full', 1, 7),
                               ('random', 1, 8), ('urandom', 1, 9), ('tty', 5, 0)):
        fields, _ = entries['dev/' + name]
        assert fields[1] == stat.S_IFCHR | 0o666, name
        assert fields[9:11] == [major, minor], name
    assert entries['lib/ld-linux-aarch64.so.1'][0][1] == stat.S_IFREG | 0o555


def with_field(entries, path, index, value):
    fields, payload = entries[path]
    changed = fields.copy()
    changed[index] = value
    return {**entries, path: (changed, payload)}


def require_rejection(check, entries, error):
    try:
        check(entries)
    except error:
        return
    raise AssertionError(f'{check.__name__} accepted the negative control')


def check_entry_controls(entries):
    check_entry(entries)
    require_rejection(check_entry, with_field(entries, 'init', 1, stat.S_IFREG | 0o400), AssertionError)
    missing = entries.copy()
    del missing['sbin/ltp/openat201']
    require_rejection(check_entry, missing, KeyError)


def check_device_controls(entries):
    check_devices(entries)
    require_rejection(check_devices, with_field(entries, 'dev/full', 10, 3), AssertionError)
    loader = with_field(entries, 'lib/ld-linux-aarch64.so.1', 1, stat.S_IFREG | 0o444)
    require_rejection(check_devices, loader, AssertionError)


def check_replay(data):
    pins = json.loads((GUEST / 'inputs.json').read_bytes())
    assert sha256(data).hexdigest() == pins['image_sha256'], 'control requires the pinned archive'
    entries = unpack(data)
    check_entry(entries)
    # Inject TBROK into a disposable copy and run the actual replay gate.
    # The production archive and lock stay untouched.
    test = 'sbin/ltp/openat201'
    entries[test] = entries[test][0], b'#!/bin/sh\nexit 2\n'
    image = encode_initramfs((name, fields[1], payload, fields[9], fields[10])
                             for name, (fields, payload) in entries.items())
    with tempfile.TemporaryDirectory(prefix='atalaya-replay-control-') as directory:
        root = Path(directory)
        runner = root / 'replay-image.sh'
        archive = root / 'control.initramfs.gz'
        copyfile(GUEST / runner.name, runner)
        archive.write_bytes(image)
        (root / 'inputs.json').write_text(json.dumps({'image_sha256': sha256(image).hexdigest()}))
        # The replay enables controllers in its own delegated scope. This
        # checker must stay outside that scope so it can verify the result.
        result = subprocess.run([
            'systemd-run', '--user', '--scope', '--slice-inherit',
            '-p', 'MemoryHigh=6G', '-p', 'MemoryMax=8G', '-p', 'Delegate=yes',
            '--', 'env', '-u', 'DISPLAY', 'taskset', '-c', '0-15', '/bin/bash', runner, archive,
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(result.stdout, end='')
    print(f'broken LTP replay rc={result.returncode}')
    assert 'LTP openat201 rc=2' in result.stdout.splitlines(), 'injected LTP result was not reached'
    assert result.returncode == 1 and 'LTP openat201 rejected rc=2' in result.stdout.splitlines()


def check_kernel(config):
    nominal = kernel_gate.assess(config, ['psi=1'])
    assert nominal['pass'], nominal
    original_pin = kernel_gate.PIN
    try:
        for symbol in ('CONFIG_UNIX98_PTYS', 'CONFIG_DEVTMPFS'):
            original = f'{symbol}=y'.encode()
            for state in (b'', f'# {symbol} is not set'.encode()):
                changed = config.replace(original, state)
                assert changed != config
                kernel_gate.PIN = sha256(changed).hexdigest()
                contrast = kernel_gate.assess(changed, ['psi=1'])
                assert contrast['pin_ok'] and not contrast['pass'], contrast
                assert f'{symbol}=y' in contrast['config_delta'], contrast
    finally:
        kernel_gate.PIN = original_pin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('control', choices=('entry', 'devices', 'kernel', 'replay'))
    parser.add_argument('artifact', type=Path)
    options = parser.parse_args()
    data = options.artifact.read_bytes()
    if options.control == 'entry':
        check_entry_controls(unpack(data))
    elif options.control == 'devices':
        check_device_controls(unpack(data))
    elif options.control == 'replay':
        check_replay(data)
    else:
        check_kernel(data)
    print(f'{options.control}: PASS')


if __name__ == '__main__':
    main()
