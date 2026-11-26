"""Regression controls for the offline image entry and bubblewrap prerequisites."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import stat

from guest_command_runtime import kernel_gate
from guest_command_runtime.image import static_arm64
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
    parser.add_argument('control', choices=('entry', 'devices', 'kernel'))
    parser.add_argument('artifact', type=Path)
    options = parser.parse_args()
    data = options.artifact.read_bytes()
    if options.control == 'entry':
        check_entry(unpack(data))
    elif options.control == 'devices':
        check_devices(unpack(data))
    else:
        check_kernel(data)
    print(f'{options.control}: PASS')


if __name__ == '__main__':
    main()
