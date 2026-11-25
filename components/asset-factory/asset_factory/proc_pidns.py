"""Opt-in test backend. All delivery uses retained kernel process descriptors.

Each job has its own child subreaper. Adopted descendants remain attributable
after the payload exits or creates another session. No process-group selector
or namespace-wide delivery is used. The outer safety scope bounds memory.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time


def _refuse(reason):
    # Import lazily: this file also runs as an isolated supervisor script.
    from asset_factory.proc import SignalRefusal
    raise SignalRefusal(reason)


def _pids(status):
    rows = [line.split()[1:] for line in status.splitlines() if line.startswith('NSpid:')]
    if len(rows) != 1 or not rows[0] or any(not x.isascii() or not x.isdecimal() or int(x) < 1 for x in rows[0]):
        _refuse('pidns_namespace_unproven')
    return tuple(map(int, rows[0]))


def _local_pids():
    return _pids(Path('/proc/self/status').read_text())


def _namespace_pids():
    local = _local_pids()
    if len(local) >= 2:
        return local
    # --mount-proc reports NSpid relative to the new procfs mount. The test
    # wrapper can retain the outer procfs at a namespace-init-held directory FD.
    # A text fixture cannot stand in for that kernel filesystem or namespace.
    root = os.environ.get('AF_PROC_PIDNS_PROC')
    if not root:
        return local
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        buffer = ctypes.create_string_buffer(256)
        if ctypes.CDLL(None, use_errno=True).fstatfs(fd, buffer) != 0 or ctypes.c_long.from_buffer(buffer).value != 0x9FA0:
            _refuse('pidns_namespace_unproven')
        if os.stat('self/ns/pid', dir_fd=fd) != os.stat('/proc/self/ns/pid'):
            _refuse('pidns_namespace_unproven')
        status_fd = os.open('self/status', os.O_RDONLY | os.O_CLOEXEC, dir_fd=fd)
        with os.fdopen(status_fd) as stream:
            return _pids(stream.read())
    finally:
        os.close(fd)


def require_namespace():
    try:
        pids = _namespace_pids()
    except (OSError, ValueError):
        _refuse('pidns_namespace_unproven')
    if len(pids) < 2 or pids[-1] != os.getpid():
        _refuse('pidns_namespace_unproven')
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        _refuse('pidns_pidfd_unavailable')
    return pids


def _identity(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return int(fields[1]), int(fields[19])


def _retain(pid, parent):
    if type(pid) is not int:
        _refuse('signal_target_not_integer')
    if pid <= 1 or pid in (os.getpid(), os.getpgrp()):
        _refuse('signal_target_unsafe')
    before = _identity(pid)
    if before[0] != parent:
        _refuse('group_parent_unproven')
    fd = os.pidfd_open(pid, 0)
    try:
        if _identity(pid) != before:
            _refuse('group_identity_changed')
        # The descriptor, not the sampled integer, is the delivery authority.
        target = Path(f'/proc/self/fdinfo/{fd}').read_text().split('Pid:', 1)[1].split()[0]
        if int(target) != pid:
            _refuse('group_identity_changed')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _deliver(fd, sig):
    try:
        signal.pidfd_send_signal(fd, sig, None, 0)
    except ProcessLookupError:
        pass
    except OSError:
        _refuse('group_drain_unproven')


def terminate_owned_child(pid):
    """Terminate a current direct child through its retained descriptor."""
    fd = _retain(pid, os.getpid())
    try:
        _deliver(fd, signal.SIGKILL)
    finally:
        os.close(fd)


class NamespaceJob:
    """Private state behind proc.Job; never delegated as a process API."""
    def __init__(self, child, fd, receipt):
        self._child, self._fd, self._receipt = child, fd, receipt
        self._result = None
        self._terminal_error = None
        self._lock = threading.RLock()
        self._creator = os.getpid()
        self._deliveries = 0

    def _retire(self):
        # A selected descriptor stays live through the final delivery call,
        # including callbacks that re-enter wait/poll on this same thread.
        if self._deliveries == 0 and self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __del__(self):
        if getattr(self, '_creator', None) == os.getpid():
            self._retire()

    def poll(self):
        with self._lock:
            return self._poll()

    def _poll(self):
        if self._result is not None:
            return self._result
        if self._terminal_error is not None:
            _refuse(self._terminal_error)
        if not select.select([self._fd], [], [], 0)[0]:
            return None
        # The supervisor writes evidence only after reaping all its children.
        try:
            try:
                info = os.waitid(os.P_PIDFD, self._fd, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            except ChildProcessError:
                # Another native waiter consumed the supervisor status. Its
                # complete drain receipt proves descriptor retirement only.
                record = json.loads(self._receipt.read_text())
                if record.get('drained') is not True or type(record.get('returncode')) is not int:
                    raise ValueError
                self._terminal_error = 'job_result_unproven'
                self._retire()
                _refuse(self._terminal_error)
            if info is None:
                return None
            self._child.wait()
            record = json.loads(self._receipt.read_text())
            if info.si_code != os.CLD_EXITED or info.si_status != 0 or record.get('drained') is not True or type(record.get('returncode')) is not int:
                raise ValueError
        except (OSError, ValueError, KeyError) as exc:
            from asset_factory.proc import SignalRefusal
            raise SignalRefusal(self._terminal_error or 'group_drain_unproven') from exc
        from asset_factory.proc import JobResult
        code = record['returncode']
        self._result = JobResult(code, 'pidns-exit', None, code,
                                 None if code == 0 else 'job_payload_failed')
        self._retire()
        return self._result

    def stop(self, sig):
        with self._lock:
            result = self._poll()
            if result is not None:
                return result
            # SIGUSR1 requests a drain; never kill the subreaper itself.
            self._deliveries += 1
            try:
                _deliver(self._fd, signal.SIGUSR1 if int(sig) == 9 else sig)
            finally:
                self._deliveries -= 1
                if self._result is not None or self._terminal_error is not None:
                    self._retire()
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            result = self.poll()
            if result is not None:
                return result
            time.sleep(.005)
        _refuse('group_drain_unproven')


def launch(job, argv, *, cwd, env, source, runtime_s):
    require_namespace()
    root = Path(job._scratch.name)
    spec = root/'spec.json'
    receipt = root/'result.json'
    spec.write_text(json.dumps({'argv': argv, 'cwd': os.fspath(cwd or Path.cwd()),
        'env': env, 'stdin': str(source), 'stdout': str(job.stdout_path),
        'stderr': str(job.stderr_path), 'runtime_s': runtime_s, 'receipt': str(receipt)}, sort_keys=True)+'\n')
    # Ready handshake: stop can never race supervisor signal-handler setup.
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    try:
        try:
            child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), str(spec), str(write_fd)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(write_fd,))
            fd = _retain(child.pid, os.getpid())
        except BaseException:
            os.close(read_fd)
            raise
    finally:
        os.close(write_fd)
    try:
        if not select.select([read_fd], [], [], 5)[0] or os.read(read_fd, 1) != b'R':
            _refuse('group_spawn_unproven')
        return NamespaceJob(child, fd, receipt)
    except BaseException:
        # The child has a bounded setup/runtime and observes parent loss. No
        # unsafe numeric delivery is introduced even on a failed handshake.
        os.close(fd)
        raise
    finally:
        os.close(read_fd)


def _children(parent):
    result = set()
    try:
        tasks = list(Path(f'/proc/{parent}/task').iterdir())
    except FileNotFoundError:
        return result
    for task in tasks:
        try:
            result.update(map(int, (task/'children').read_text().split()))
        except FileNotFoundError:
            pass
    return result


def _drain():
    """Freeze, discover, drain and reap this subreaper's tree, including setsid."""
    retained = {}
    deadline = time.monotonic()+3
    try:
        while time.monotonic() < deadline:
            pending = [os.getpid()]
            while pending:
                parent = pending.pop()
                for pid in _children(parent):
                    if pid not in retained:
                        try:
                            fd = _retain(pid, parent)
                        except ProcessLookupError:
                            continue
                        except FileNotFoundError:
                            continue
                        retained[pid] = fd
                        _deliver(fd, signal.SIGSTOP)
                    pending.append(pid)
            for fd in retained.values():
                _deliver(fd, signal.SIGKILL)
            for fd in retained.values():
                try:
                    os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG)
                except ChildProcessError:
                    pass
            if not _children(os.getpid()):
                return
            time.sleep(.005)
        _refuse('group_drain_unproven')
    finally:
        for fd in retained.values():
            os.close(fd)


def _supervise(spec_path, ready_fd):
    # Isolated script execution still imports the same public refusal type.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    require_namespace()
    parent = os.getppid()
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        _refuse('group_spawn_unproven')
    requested = []
    def request(sig, frame):
        requested.append(signal.SIGKILL if sig == signal.SIGUSR1 else sig)
    for sig in (signal.SIGUSR1, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request)
    spec = json.loads(Path(spec_path).read_text())
    with open(spec['stdin'], 'rb') as source, open(spec['stdout'], 'ab', buffering=0) as out, open(spec['stderr'], 'ab', buffering=0) as err:
        child = subprocess.Popen(spec['argv'], cwd=spec['cwd'], env=spec['env'],
            stdin=source, stdout=out, stderr=err, start_new_session=True)
    fd = _retain(child.pid, os.getpid())
    try:
        os.write(ready_fd, b'R')
        os.close(ready_fd)
        deadline = time.monotonic()+spec['runtime_s']
        stopped = False
        while child.poll() is None:
            if not stopped and (requested or os.getppid() != parent or time.monotonic() >= deadline):
                _deliver(fd, requested[0] if requested else signal.SIGKILL)
                stopped = True
                stop_deadline = time.monotonic()+2
            if stopped and time.monotonic() >= stop_deadline:
                _deliver(fd, signal.SIGKILL)
            time.sleep(.005)
        code = child.returncode
        _drain()
        target = Path(spec['receipt'])
        stage = target.with_suffix('.tmp')
        with stage.open('w') as output:
            json.dump({'drained': True, 'returncode': code}, output, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(stage, target)
    finally:
        os.close(fd)


if __name__ == '__main__':
    _supervise(sys.argv[1], int(sys.argv[2]))
    # The receipt and descriptor disposal are already complete. Python's
    # interpreter teardown restores default signal handlers; a concurrent drain
    # request there could turn proven completion into -SIGUSR1. Exit atomically
    # through the kernel, without exposing that handler-free teardown window.
    os._exit(0)
