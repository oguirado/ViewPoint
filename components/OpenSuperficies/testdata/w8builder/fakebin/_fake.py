#!/usr/bin/env python3
"""Inert W8 operator-command fixtures. All writes require an isolated scratch root.

No command is forwarded to the host. Unknown commands and missing isolation fail 2.
OSW_FAKE_FAIL=<command> and OSW_FAKE_RC inject a normalized failure.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import sys


def main():
    if len(sys.argv) < 2:
        return 2
    name, args = sys.argv[1], sys.argv[2:]
    root_value = os.environ.get('OSW_ROOT')
    state_value = os.environ.get('OSW_FAKE_STATE')
    if not root_value or not state_value:
        print('fakebin requires OSW_ROOT and OSW_FAKE_STATE', file=sys.stderr)
        return 2
    root = Path(root_value).resolve()
    state_dir = Path(state_value).resolve()
    if root == Path('/') or state_dir == Path('/'):
        return 2
    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / 'state.json'
    state = json.loads(db.read_text()) if db.exists() else {
        'user': False, 'locked': False, 'kvm': False,
        'linger': False, 'user_active': False, 'docker_active': False,
    }
    with (state_dir / 'calls').open('a') as log:
        log.write(name + (' ' + ' '.join(args) if args else '') + '\n')
    if name == os.environ.get('OSW_FAKE_FAIL'):
        code = int(os.environ.get('OSW_FAKE_RC', '5'))
        return code if code in (0, 2, 3, 4, 5, 6, 7) else 5
    uid = os.environ.get('OSW_FAKE_UID', '10001')
    version = os.environ.get('OSW_FAKE_VERSION', '5:29.6.2-1~ubuntu.24.04~noble')

    def safe(value):
        p = Path(value).resolve()
        if p != root and root not in p.parents:
            raise ValueError('fake path outside OSW_ROOT')
        return p

    etc = safe(os.environ.get('OSW_ETC', str(root / 'etc')))
    home = safe(os.environ.get('OSW_HOME', str(root / 'var/lib/osworker')))
    run = safe(os.environ.get('OSW_RUN_ROOT', str(root / 'run')))
    cgroup = safe(os.environ.get('OSW_CGROUP_ROOT', str(root / 'sys/fs/cgroup')))

    def save():
        db.write_text(json.dumps(state, sort_keys=True) + '\n')

    def user_bus():
        runtime = run / 'user' / uid
        runtime.mkdir(parents=True, exist_ok=True)
        runtime.chmod(0o700)
        bus = runtime / 'bus'
        if not bus.exists():
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(bus))
            sock.close()
        controllers = cgroup / 'user.slice' / ('user-' + uid + '.slice') / ('user@' + uid + '.service') / 'cgroup.controllers'
        controllers.parent.mkdir(parents=True, exist_ok=True)
        controllers.write_text('cpu cpuset io memory pids\n')

    if name == 'id':
        if args == ['-u']:
            print(os.environ.get('OSW_FAKE_EUID', '0'))
        elif args == ['-u', 'osworker'] and state['user']:
            print(uid)
        elif args in (['-nG', 'osworker'], ['-Gn', 'osworker']) and state['user']:
            print('osworker' + (' kvm' if state['kvm'] else '') + os.environ.get('OSW_FAKE_EXTRA_GROUPS', ''))
        else:
            return 2
    elif name == 'uname' and args == ['-m']:
        print(os.environ.get('OSW_FAKE_ARCH', 'aarch64'))
    elif name == 'getent':
        if args == ['passwd', 'osworker'] and state['user']:
            print('osworker:x:' + uid + ':' + uid + '::' + str(home) + ':/bin/sh')
        elif args == ['group', 'kvm']:
            print('kvm:x:994:' + ('osworker' if state['kvm'] else ''))
        else:
            return 2
    elif name == 'dpkg-query' and args == ['-W', '-f=${Version}', 'docker-ce']:
        print(version)
    elif name == 'apt-cache' and args == ['madison', 'docker-ce-rootless-extras']:
        available = os.environ.get('OSW_FAKE_AVAILABLE_VERSION', version)
        print('docker-ce-rootless-extras | ' + available + ' | fixture-only')
    elif name == 'apt-get':
        if args != ['install', '-y', '--no-install-recommends', 'uidmap', 'dbus-user-session', 'slirp4netns', 'docker-ce-rootless-extras=' + version]:
            return 2
        state['package_version'] = version
        save()
    elif name == 'useradd':
        if not args or args[-1] != 'osworker':
            return 2
        state['user'] = True
        home.mkdir(parents=True, exist_ok=True)
        etc.mkdir(parents=True, exist_ok=True)
        for sub in ('subuid', 'subgid'):
            (etc / sub).touch(exist_ok=True)
        save()
    elif name == 'usermod':
        if not state['user'] or not args or args[-1] != 'osworker':
            return 2
        if args == ['-p', '*', 'osworker']:
            state['locked'] = True
        elif args == ['-aG', 'kvm', 'osworker']:
            state['kvm'] = True
        else:
            groups = {'--add-subuids': 'subuid', '--add-subgids': 'subgid'}
            changed = False
            for flag, sub in groups.items():
                if flag in args:
                    i = args.index(flag)
                    if i + 1 >= len(args) or args[i + 1] != '165536-231071':
                        return 2
                    with (etc / sub).open('a') as f:
                        f.write('osworker:165536:65536\n')
                    changed = True
            if not changed:
                return 2
        save()
    elif name == 'userdel' and args in (['osworker'], ['-r', 'osworker']):
        state['user'] = False
        if '-r' in args and home.exists():
            shutil.rmtree(home)
        save()
    elif name == 'gpasswd' and args == ['-d', 'osworker', 'kvm']:
        state['kvm'] = False
        save()
    elif name == 'passwd' and args == ['-S', 'osworker']:
        print('osworker ' + ('L' if state['locked'] else 'P') + ' fixture')
    elif name == 'loginctl':
        if args == ['enable-linger', 'osworker']:
            state['linger'] = True
            state['user_active'] = True
            user_bus()
        elif args == ['disable-linger', 'osworker']:
            state['linger'] = False
        elif args == ['show-user', 'osworker', '--property=Linger', '--value']:
            print('yes' if state['linger'] else 'no')
        else:
            return 2
        save()
    elif name == 'systemd-run':
        prefix = ['--machine=osworker@', '--user', '--wait', '--pipe', '--quiet']
        if args[:len(prefix)] != prefix or len(args) <= len(prefix):
            return 2
        # Re-enter this inert dispatcher; never execute the requested host command.
        sys.argv = [sys.argv[0]] + args[len(prefix):]
        return main()
    elif name == 'systemctl':
        if args == ['daemon-reload']:
            pass
        elif args == ['stop', 'user@' + uid + '.service']:
            state['user_active'] = state['docker_active'] = False
        elif args == ['restart', 'user@' + uid + '.service']:
            state['user_active'] = True
            user_bus()
        elif args == ['is-active', '--quiet', 'user@' + uid + '.service']:
            return 0 if state['user_active'] else 5
        elif args == ['is-active', '--quiet', 'docker']:
            return 0
        elif args in (['--user', 'enable', '--now', 'docker'], ['--user', 'start', 'docker']):
            state['docker_active'] = True
        elif args == ['--user', 'disable', '--now', 'docker']:
            state['docker_active'] = False
        elif args == ['--user', 'daemon-reload']:
            pass
        elif args == ['--user', 'show', 'docker', '--property=CPUAffinity', '--value']:
            print('10-14')
        else:
            return 2
        save()
    elif name == 'dockerd-rootless-setuptool.sh':
        if args not in (['install', '--skip-iptables'], ['uninstall']):
            return 2
    elif name == 'install':
        if '-d' not in args:
            return 2
        mode = int(args[args.index('-m') + 1], 8) if '-m' in args else 0o755
        i = 0
        while i < len(args):
            a = args[i]
            if a in ('-m', '-o', '-g'):
                i += 2
                continue
            if a == '-d':
                i += 1
                continue
            p = safe(a)
            p.mkdir(parents=True, exist_ok=True)
            p.chmod(mode)
            i += 1
    elif name == 'chown':
        if len(args) != 2 or args[0] != 'osworker:osworker':
            return 2
        safe(args[1])
    elif name == 'systemd-tmpfiles' and len(args) == 2 and args[0] == '--create':
        safe(args[1])
        p = run / 'opensuperficies'
        p.mkdir(parents=True, exist_ok=True)
        p.chmod(0o750)
    elif name == 'stat' and len(args) == 3 and args[0] == '-c':
        p = safe(args[2])
        if not p.exists():
            return 5
        if args[1] == '%a':
            print(format(stat.S_IMODE(p.stat().st_mode), 'o'))
        elif args[1] == '%a:%U:%G' and p == run / 'opensuperficies':
            print(format(stat.S_IMODE(p.stat().st_mode), 'o') + ':osworker:admin')
        else:
            return 2
    elif name == 'find' and args == [str(home), '-xdev', '-ignore_readdir_race', '-path', str(home) + '/.local/share/docker', '-prune', '-o', '-uid', '0', '-type', 'f', '-print']:
        # Fixture ownership is virtual; no chown is performed by this test double.
        pass
    elif name == 'getfacl' and len(args) == 2 and args[0] == '-cp':
        safe(args[1])
        print('user::rwx\ngroup::---\nother::---')
    elif name == 'runuser':
        if args[:3] != ['-u', 'admin', '--']:
            return 2
        return 5  # The fake boundary is EACCES; no Python snippet is executed.
    elif name == 'pgrep' and args == ['-u', 'osworker']:
        if state['user_active'] or os.environ.get('OSW_FAKE_PROCESSES_REMAIN') == 'yes':
            print('4242')
        else:
            return 1  # Native no-match fixture status, consumed by the shell guard.
    elif name == 'pkill' and args == ['-KILL', '-u', 'osworker']:
        state['user_active'] = state['docker_active'] = False
        save()
    elif name == 'sleep':
        return 0  # Bounded polling is deterministic and immediate in fixtures.
    elif name == 'systemd-cat' and args == ['-t', 'w8-capsule']:
        with (state_dir / 'journal').open('a') as f:
            f.write(sys.stdin.read(4097))
    elif name == 'docker':
        if args[:1] == ['--host'] and len(args) >= 2:
            args = args[2:]
        if not state['docker_active']:
            return 4
        if args == ['info', '--format', '{{json .}}']:
            print(json.dumps({'SecurityOptions':['name=rootless'], 'DriverStatus':[['driver-type','io.containerd.snapshotter.v1']], 'CgroupVersion':'2'}))
        elif args[:2] == ['image', 'inspect'] and args[-1:] == ['{{.Id}}']:
            # Test identity, never a real or approved production digest.
            print('sha256:' + __import__('hashlib').sha256(b'w8 fake image').hexdigest())
        elif args[:2] == ['image', 'inspect'] and args[-1:] == ['{{json .Config.Entrypoint}}']:
            print('["/usr/bin/tini","-s","--"]')
        elif args[:1] == ['run'] and args[-1:] == ['id']:
            print('uid=10001(worker) gid=10001(worker) groups=10001(worker)')
        else:
            return 2
    else:
        print('unsupported fake command: ' + name, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    try:
        rc = main()
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print('fakebin: ' + str(exc), file=sys.stderr)
        rc = 5
    sys.exit(rc if rc in (0, 2, 3, 4, 5, 6, 7) or (sys.argv[1] == 'pgrep' and rc == 1) else 5)
