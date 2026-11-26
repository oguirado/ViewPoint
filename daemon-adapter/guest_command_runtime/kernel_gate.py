"""Check the pinned ARM64 guest config before admitting the lanes image."""

import argparse
import hashlib
import json
import re
from pathlib import Path


PIN = '9742b4917898592efabdc0ecc6e0419ab2b1be4232c59b1def47a7d4e3b1ff8f'
REQUIRED = (
    'NAMESPACES', 'UTS_NS', 'IPC_NS', 'PID_NS', 'NET_NS', 'USER_NS',
    'OVERLAY_FS', 'CGROUPS', 'MEMCG', 'CGROUP_PIDS', 'PSI', 'SYSFS',
    'SYSCTL', 'PROC_FS', 'TMPFS', 'SECCOMP', 'SECCOMP_FILTER', 'UNIX',
    'BLK_DEV_INITRD', 'VSOCKETS', 'VIRTIO_VSOCKETS',
)
PSI_DEFAULT = 'CONFIG_PSI_DEFAULT_DISABLED'
CONFIG = re.compile(r'(CONFIG_[A-Z0-9_]+)=(.*)|# (CONFIG_[A-Z0-9_]+) is not set')


def assess(raw, boot_args=()):
    digest = hashlib.sha256(raw).hexdigest()
    states = {}
    duplicates = set()
    for line in raw.decode('utf-8').splitlines():
        match = CONFIG.fullmatch(line)
        if match:
            assigned, value, unset = match.groups()
            key = assigned or unset
            if key in states:
                duplicates.add(key)
            states[key] = value if assigned else 'n'
    symbols = ['CONFIG_' + name for name in REQUIRED]
    inspected = {name: states.get(name, 'AUSENTE') for name in [*symbols, PSI_DEFAULT]}
    delta = [f'{key}=y' for key in symbols if inspected[key] != 'y']
    psi_mode = inspected[PSI_DEFAULT]
    if psi_mode not in ('y', 'n'):
        delta.append('# CONFIG_PSI_DEFAULT_DISABLED is not set')
    args = [arg for arg in boot_args if arg.startswith('psi=') or arg == 'psi']
    psi_ok = args == ['psi=1'] or psi_mode == 'n' and not args
    conflicts = sorted(duplicates.intersection(inspected))
    return {
        'sha256': digest,
        'pin_ok': digest == PIN,
        'symbols': inspected,
        'duplicate_symbols': conflicts,
        'config_delta': delta,
        'required_boot_arg': 'psi=1' if psi_mode == 'y' else None,
        'boot_args_ok': psi_ok,
        'pass': digest == PIN and not delta and not conflicts and psi_ok,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--boot-args', default='')
    options = parser.parse_args()
    try:
        receipt = assess(options.config.read_bytes(), options.boot_args.split())
    except (OSError, UnicodeError) as error:
        receipt = {'pass': False, 'error': str(error)}
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt['pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
