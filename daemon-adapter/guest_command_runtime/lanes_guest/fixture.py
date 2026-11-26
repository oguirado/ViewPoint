"""Offline isolation witness. Run via the delegated systemd scope in LIVE_PLAN."""

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from guest_command_runtime.lanes import LaneDenied, LaneIngress
from guest_command_runtime.lanes_image import GUEST, SYSTEM


def put(path, value):
    path.write_text(f'{value}\n')


def stage(root):
    for name in ('bin', 'etc', 'etc/git-template', 'objects'):
        (root / name).mkdir(parents=True)
    for name, source in SYSTEM.items():
        target = root / (name if name.startswith('lib/') else f'bin/{name}')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((Path('/') / source).read_bytes())
        target.chmod(0o555)
    for applet in ('sh', 'sleep', 'id'):
        (root / 'bin' / applet).symlink_to('busybox')
    for source, output in [('enter.c', 'lane-enter'), ('probe.c', 'probe')]:
        subprocess.run(['/usr/bin/cc', '-O2', '-Wall', '-Wextra', '-Werror', '-static',
                        str(GUEST / source), '-o', str(root / 'bin' / output)], check=True)


def peer(listener, address, leaf, uid):
    pid = os.fork()
    if pid == 0:
        try:
            listener.close()
            put(leaf / 'cgroup.procs', os.getpid())
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.connect(address)
                result = channel.recv(1)
                os._exit(0 if result == b'!' else 1)
        except BaseException:
            os._exit(1)
    return pid


def verify_ingress(top, start):
    auth = LaneIngress(top)
    effects = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        # Tests use an abstract address that lane shells cannot reach across netns.
        address = b'\0atalaya-fixture-' + os.urandom(16).hex().encode()
        listener.bind(address)
        listener.listen()
        listener.settimeout(4)
        for lane_number in (0, 1):
            for claimed, leaf in [(lane_number, top / str(lane_number)),
                                  (lane_number, top / str(1 - lane_number))]:
                pid = peer(listener, address, leaf, 10000 + lane_number)
                try:
                    connection, _ = listener.accept()
                    with connection:
                        for request in ({'lane_id': 1 - claimed},
                                        {'params': {'lane_id': 1 - claimed}}):
                            try:
                                auth.dispatch(claimed, connection, request,
                                              lambda lane, _: effects.append(lane.number))
                            except LaneDenied:
                                pass
                            else:
                                raise AssertionError('spoof dispatched')
                        if leaf == top / str(lane_number):
                            before = len(effects)
                            try:
                                auth.dispatch(1 - lane_number, connection, {},
                                              lambda lane, _: effects.append(lane.number))
                            except LaneDenied:
                                pass
                            else:
                                raise AssertionError('wrong UID dispatched')
                            assert len(effects) == before
                            def launch(lane, request):
                                start(lane.number)
                                effects.append(lane.number)
                            auth.dispatch(claimed, connection, {}, launch)
                        else:
                            try:
                                auth.dispatch(claimed, connection, {},
                                              lambda lane, _: effects.append(lane.number))
                            except LaneDenied:
                                pass
                            else:
                                raise AssertionError('wrong cgroup dispatched')
                        connection.sendall(b'!')
                except BaseException:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                    raise
                assert os.waitpid(pid, 0)[1] == 0
    assert effects == [0, 1]
    print('SO_PEERCRED and cgroup inode: two valid dispatches, forged/wrong peers zero effects')


def run_fixture(top, root):
    image = root / 'root'
    stage(image)
    sentinel = root / 'ungranted'
    sentinel.write_text('confined\n')
    before = [
        sentinel, Path('/proc/1/root') / sentinel.relative_to('/'), top / '1/cgroup.procs',
    ]
    for path in before:
        with path.open('w' if path.name == 'cgroup.procs' else 'r'):
            pass
    subprocess.run([image / 'bin/probe', 'baseline'], check=True)
    print('negative contrast: unconfined open and ptrace succeed')
    processes = []
    workspaces = []
    with ExitStack() as stack:
        git_env = {'PATH': '/usr/bin:/bin', 'LANG': 'C',
                   'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        object_repo = root / 'git-objects'
        subprocess.run(['/usr/bin/git', 'init', '-q', '--bare', object_repo], env=git_env, check=True)
        object_id = subprocess.check_output(
            ['/usr/bin/git', '--git-dir', object_repo, 'hash-object', '-w', '--stdin'],
            input=b'pinned object\n', env=git_env,
        ).decode().strip()
        pinned = Path(object_id[:2], object_id[2:])
        (image / 'objects' / pinned.parent).mkdir()
        (image / 'objects' / pinned).write_bytes((object_repo / 'objects' / pinned).read_bytes())

        def start(index):
            workspace = root / f'clone-{index}'
            workspace.mkdir(mode=0o700)
            subprocess.run(['/usr/bin/git', '-C', workspace, 'init', '-q'], env=git_env, check=True)
            (workspace / 'escape-link').symlink_to(sentinel)
            for parent, directories, files in os.walk(workspace):
                for directory in directories:
                    os.chown(Path(parent) / directory, 10000 + index, 10000 + index)
                for file in files:
                    os.chown(Path(parent) / file, 10000 + index, 10000 + index, follow_symlinks=False)
            os.chown(workspace, 10000 + index, 10000 + index)
            alternate = workspace / '.git/objects/info/alternates'
            put(alternate, '/objects')
            alternate.chmod(0o444)
            workspaces.append(workspace)
            command = (
                f'/bin/git cat-file blob {object_id} && /bin/git rev-parse --show-toplevel && '
                'printf ready > /work/ready && '
                'while [ ! -e /work/go ]; do /bin/sleep 0.02; done && '
                f'/bin/probe {index} && /bin/id -u'
            )
            process = subprocess.Popen(
                ['/bin/sh', GUEST / 'run.sh', str(index), image, workspace, top / str(index),
                 '/bin/sh', '-ec', command],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, close_fds=True,
            )
            stack.callback(cleanup, process)
            processes.append(process)

        def cleanup(child):
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()

        verify_ingress(top, start)
        until = time.monotonic() + 5
        while not all((work / 'ready').is_file() for work in workspaces):
            for index, process in enumerate(processes):
                if process.poll() is not None:
                    raise AssertionError(f'lane {index} exited early: {process.communicate()[0]}')
            if time.monotonic() >= until:
                raise TimeoutError('lanes failed to overlap')
            time.sleep(0.01)
        for i, process in enumerate(processes):
            assert str(process.pid) in (top / str(i) / 'cgroup.procs').read_text().split()
        for work in workspaces:
            (work / 'go').touch()
        for index, process in enumerate(processes):
            output, _ = process.communicate(timeout=5)
            print(f'lane {index}:\n{output}', end='')
            if process.returncode or output.splitlines()[-1] != str(10000 + index):
                raise AssertionError(f'lane {index} syscall or terminal witness failed')
    until = time.monotonic() + 2
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        if all('populated 0' in (top / str(index) / 'cgroup.events').read_text().splitlines()
               for index in (0, 1)):
            break
        if time.monotonic() >= until:
            raise AssertionError('lane cgroup did not empty after both terminals')
        time.sleep(0.01)
    assert sentinel.read_text() == 'confined\n'
    print('overlap, independent Git workspaces and empty cgroups verified')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', type=Path)
    options = parser.parse_args()
    if options.child:
        assert os.getpid() == 1 and os.getuid() == 0
        assert Path('/proc/sys/fs/protected_symlinks').read_text().strip() == '1'
        assert int(Path('/proc/sys/fs/protected_hardlinks').read_text()) == 1
        with tempfile.TemporaryDirectory(prefix='atalaya-fixture-') as directory:
            run_fixture(options.child, Path(directory))
        return
    assert os.getuid() != 0
    scope = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().strip().removeprefix('0::/')
    assert scope.name.endswith('.scope') and (scope / 'memory.max').read_text().strip() == '8589934592'
    manager = scope / 'fixture-manager'
    manager.mkdir()
    put(manager / 'cgroup.procs', os.getpid())
    put(scope / 'cgroup.subtree_control', '+memory +pids')
    lanes = []
    try:
        for index in (0, 1):
            leaf = scope / str(index)
            leaf.mkdir(mode=0o700)
            lanes.append(leaf)
            put(leaf / 'memory.max', 67108864)
            put(leaf / 'memory.swap.max', 0)
            put(leaf / 'pids.max', 24)
        subprocess.run(['/usr/bin/unshare', '-Urpf', '--map-auto', '--mount-proc',
                        '--kill-child', sys.executable, '-B', str(Path(__file__).resolve()),
                        '--child', scope], timeout=30, check=True)
    finally:
        for leaf in lanes:
            put(leaf / 'cgroup.kill', 1)
            leaf.rmdir()


if __name__ == '__main__':
    main()
