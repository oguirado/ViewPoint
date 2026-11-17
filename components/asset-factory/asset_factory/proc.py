"""External jobs owned by transient systemd user services.

Only generated unit names cross the control boundary. Job processes are children
of the user manager, never of this module. Deliberate cgroup escape by a job is
outside the contract (PROC-B, OUT_OF_CONTRACT). External replacement of an
owned unit by another controller using the identical name is also
OUT_OF_CONTRACT (P8-NAME): names embed uuid4's 122 random bits. Reuse requires
another controller deliberately creating the same af-job-* name; this helper
owns locally generated names, not hostile controllers of the user manager.
No automatic backend or numeric-signal fallback exists.
An explicitly selected test backend requires kernel proof of a nested PID
namespace; its child supervisor owns retained process descriptors.

Successful units remain active/exited and failed units remain failed until their
exit evidence is read. We deliberately do not use --collect: it can discard a
short-lived failure before show observes it. stop/reset-failed then release the
transient unit. RuntimeMaxSec bounds abandoned running jobs.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
import json
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
import weakref


class SignalRefusal(ValueError):
    rc = 3
    def __init__(self, code):
        self.code = code
        super().__init__(code)

    def as_dict(self):
        return {'rc': self.rc, 'status': 'INCOMPLETE', 'reason_codes': [self.code]}


@dataclass(frozen=True)
class MemoryLimits:
    high: int
    maximum: int
    swap: int = 0

    def __post_init__(self):
        if (type(self.high) is not int or type(self.maximum) is not int or
                type(self.swap) is not int or not 0 < self.high <= self.maximum or self.swap != 0):
            raise SignalRefusal('memory_limits_unproven')


@dataclass(frozen=True)
class JobResult:
    returncode: int
    systemd_result: str
    memory_peak_bytes: int | None
    main_returncode: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _Creation:
    unit: str
    runner: object
    owner: int
    active: bool = False


# Object identity, not a caller-writable field or a name pattern, is authority.
_CREATIONS = weakref.WeakKeyDictionary()
# Active transactions survive discarded handles and lost manager replies.
_LIVE_JOBS = set()


class _OwnedScratch(tempfile.TemporaryDirectory):
    def __init__(self, **kwargs):
        self._creator = os.getpid()
        super().__init__(**kwargs)

    def cleanup(self):
        if self._creator == os.getpid():
            super().cleanup()


def _after_fork():
    # TemporaryDirectory's weak finalizer is independent of Job.__del__.
    # Detach the inherited copy; only the parent's copy owns file disposal.
    for job in list(_CREATIONS):
        scratch = getattr(job, '_scratch', None)
        if scratch is not None:
            scratch._finalizer.detach()


os.register_at_fork(after_in_child=_after_fork)


TEST_ENVIRONMENT = ('Isolated tests require AF_PROC_BACKEND=pidns, '
                    'AF_PROC_PIDNS_PROC pointing at the outer kernel /proc handle '
                    '(for example /proc/1/fd/9 with 9</proc before unshare), and '
                    'PYTHONPATH=.:tests from the component root. '
                    'The production systemd backend never falls back to pidns.')


def check_test_environment(*, require_pythonpath=True):
    """Explain missing isolation configuration before discovery fans out."""
    if Path('/proc/1/comm').read_text().strip() == 'systemd':
        return
    if os.environ.get('AF_PROC_BACKEND') != 'pidns':
        raise SignalRefusal('test_backend_environment')
    from asset_factory import proc_pidns
    proc_pidns.require_namespace()
    if require_pythonpath:
        root = Path(__file__).resolve().parents[1]
        paths = {Path(p).resolve() for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p}
        if not {root, root/'tests'} <= paths:
            raise SignalRefusal('test_backend_environment')


class SystemdRunner:
    """Control commands only; the user manager starts all job payloads."""
    def run(self, argv, *, timeout):
        # A namespace's mapped root must never ask the host manager to start work.
        try:
            if Path('/proc/1/comm').read_text().strip() != 'systemd':
                raise SignalRefusal('systemd_user_manager_unavailable')
            return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SignalRefusal('systemd_user_manager_unavailable') from exc


_RUNNER = ContextVar('asset_factory_systemd_runner', default=None)
_SEAL = object()
_UNIT = re.compile(r'af-job-[0-9a-f]{32}\.service', re.ASCII)
_PROPERTIES = 'LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,MemoryPeak'


@contextmanager
def using_runner(runner):
    """Explicit, local test injection; never selected by an environment flag."""
    token = _RUNNER.set(runner)
    try:
        yield runner
    finally:
        _RUNNER.reset(token)


def _remaining(deadline):
    if deadline is None:
        return 5
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired('systemd transaction', 0)
    return min(5, remaining)


def _unit_call(runner, action, unit, *options, deadline=None):
    if type(unit) is not str or _UNIT.fullmatch(unit) is None:
        raise SignalRefusal('unit_name_invalid')
    if action not in ('show', 'stop', 'kill', 'reset-failed'):
        raise SignalRefusal('unit_operation_invalid')
    if action != 'show' and not any(record.active and record.owner == os.getpid()
            and record.unit == unit and record.runner is runner
            for record in tuple(_CREATIONS.values())):
        raise SignalRefusal('group_not_owned')
    reply = runner.run(['systemctl', '--user', action, *options, '--', unit],
                       timeout=_remaining(deadline))
    if deadline is not None:
        _remaining(deadline)
    return reply


def _owned(job):
    if type(job) is not Job or getattr(job, '_seal', None) is not _SEAL:
        # Numeric identities cannot be upgraded to service authority.
        if type(job) is not int:
            raise SignalRefusal('signal_target_not_integer')
        if job <= 1 or job in (os.getpid(), os.getpgrp()):
            raise SignalRefusal('signal_target_unsafe')
        raise SignalRefusal('group_not_owned')
    record = _CREATIONS.get(job)
    if record is None or record.owner != os.getpid():
        raise SignalRefusal('group_not_owned')
    if type(job.unit_name) is not str or _UNIT.fullmatch(job.unit_name) is None:
        raise SignalRefusal('unit_name_invalid')
    if job.unit_name != record.unit or job._runner is not record.runner:
        raise SignalRefusal('group_not_owned')
    return job


class Job:
    """Composed service handle. No process object or numeric target is exposed."""
    __slots__ = ('_unit_name', '_runner', '_owner', '_seal', '_result', '_scratch',
                 '_stdout_path', '_stderr_path', '_files', '_argv', '_text', '_encoding', '_backend',
                 '_pending', '_terminal_error', '_disposing', '_retired', '_cleanup_errors', '__weakref__')

    def __init__(self, unit_name, runner, *, seal=None):
        if seal is not _SEAL or type(unit_name) is not str or _UNIT.fullmatch(unit_name) is None:
            raise SignalRefusal('group_spawn_unproven')
        self._unit_name, self._runner = unit_name, runner
        self._owner, self._seal, self._result = os.getpid(), seal, None
        self._files = []
        self._backend = None
        self._pending, self._terminal_error, self._disposing = None, None, False
        self._retired, self._cleanup_errors = False, []
        _CREATIONS[self] = _Creation(unit_name, runner, self._owner)

    def __del__(self):
        # File disposal only. Losing a Python handle never issues a unit action.
        if getattr(self, '_owner', None) != os.getpid():
            return
        for stream in getattr(self, '_files', ()):
            stream.close()
        scratch = getattr(self, '_scratch', None)
        if scratch is not None:
            scratch.cleanup()

    @property
    def unit_name(self): return self._unit_name
    @property
    def stdout_path(self): return self._stdout_path
    @property
    def stderr_path(self): return self._stderr_path
    @property
    def result(self): return self._result
    @property
    def returncode(self): return None if self._result is None else self._result.returncode

    def wait(self, timeout=None):
        _owned(self)
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while True:
            # A previously published result needs no manager transaction.
            if self._result is not None:
                return self.returncode
            code = poll_exit(self, deadline=deadline)
            if code is not None:
                if deadline is not None:
                    _remaining(deadline)
                return code
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self._argv, timeout)
            time.sleep(.01 if deadline is None else min(.01, max(0, deadline-time.monotonic())))

    def stop(self, sig=signal.SIGKILL, *, _deadline=None):
        _owned(self)
        if self._result is not None:
            return self._result.returncode
        # wait continues to report the terminal refusal; cleanup is idempotent.
        if self._retired:
            return None
        if not isinstance(sig, (int, signal.Signals)) or isinstance(sig, bool) or int(sig) not in (2, 9, 15):
            raise SignalRefusal('signal_target_unsafe')
        if self._backend is not None:
            try:
                self._result = self._backend.stop(sig)
            except SignalRefusal as exc:
                if self._backend._fd is None:
                    self._retired, self._terminal_error = True, exc.code
                raise
            return self.returncode
        # Cleanup has its own total bound, independent of an expired wait.
        deadline = time.monotonic()+5 if _deadline is None else _deadline
        # First observe: a completed unit requires disposal, not another signal.
        if poll_exit(self, deadline=deadline) is not None:
            return self.returncode
        reply = _unit_call(self._runner, 'kill', self.unit_name,
                           '--kill-whom=all', f'--signal={int(sig)}', deadline=deadline)
        if reply.returncode != 0:
            # A concurrent exit is accepted only with complete unit evidence.
            if poll_exit(self, deadline=deadline) is None:
                raise SignalRefusal('group_drain_unproven')
            return self.returncode
        try:
            return self.wait(timeout=min(3, _remaining(deadline)))
        except subprocess.TimeoutExpired:
            if int(sig) == 9:
                raise SignalRefusal('group_drain_unproven') from None
            return self.stop(signal.SIGKILL, _deadline=deadline)


def poll_exit(child, *, deadline=None):
    job = _owned(child)
    if job._result is not None:
        return job.returncode
    if job._terminal_error is not None:
        raise SignalRefusal(job._terminal_error)
    if job._disposing:
        raise SignalRefusal('job_control_reentrant')
    if job._backend is not None:
        try:
            job._result = job._backend.poll()
        except SignalRefusal as exc:
            if job._backend._fd is None:
                job._retired, job._terminal_error = True, exc.code
            raise
        return job.returncode
    if job._pending is None:
        reply = _unit_call(job._runner, 'show', job.unit_name, f'--property={_PROPERTIES}', deadline=deadline)
        if reply.returncode != 0:
            raise SignalRefusal('systemd_user_manager_unavailable')
        state = dict(line.split('=', 1) for line in reply.stdout.splitlines() if '=' in line)
        if state.get('LoadState') != 'loaded':
            raise SignalRefusal('group_lifetime_unproven')
        if state.get('SubState') not in ('exited', 'failed', 'dead'):
            return None
        job._pending = _exit_evidence(state)
    result, error = job._pending
    # systemd owns descendant cleanup, including independent child sessions.
    # Returncode stays unset until the stop transaction has completed.
    _retire_unit(job, deadline=deadline)
    job._terminal_error, job._result = error, result
    if error is not None:
        raise SignalRefusal(error)
    return job.returncode


def _creation_evidence(job, state):
    record = _CREATIONS[job]
    payload = {'unit': record.unit, 'owner': record.owner, 'state': state,
               'cleanup_errors': list(job._cleanup_errors)}
    target = Path(job._scratch.name)/'creation.json'
    staged = target.with_suffix('.tmp')
    with staged.open('w') as stream:
        stream.write(json.dumps(payload, sort_keys=True, separators=(',', ':'))+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(staged, target)


def _retire_unit(job, *, deadline=None):
    _owned(job)
    job._disposing = True
    errors = []
    try:
        # reset-failed is attempted even if stop fails. An absent unit can
        # legitimately make either action return nonzero; show is the witness.
        for action in ('stop', 'reset-failed'):
            try:
                _unit_call(job._runner, action, job.unit_name, deadline=deadline)
            except (SignalRefusal, subprocess.TimeoutExpired, OSError) as exc:
                errors.append(getattr(exc, 'code', type(exc).__name__))
        retired = _unit_call(job._runner, 'show', job.unit_name,
                             '--property=LoadState', deadline=deadline)
        if retired.returncode != 0 or 'LoadState=not-found' not in retired.stdout.splitlines():
            raise SignalRefusal('group_drain_unproven')
        record = _CREATIONS[job]
        _creation_evidence(job, 'retired')
        _CREATIONS[job] = _Creation(record.unit, record.runner, record.owner)
        job._retired = True
        _LIVE_JOBS.discard(job)
    finally:
        job._cleanup_errors.extend(errors)
        job._disposing = False


def cleanup(job):
    """Best effort owned cleanup, recording errors without replacing a result."""
    try:
        _owned(job).stop()
    except Exception as exc:
        reason = getattr(exc, 'code', type(exc).__name__)
        job._cleanup_errors.append(reason)
        return list(job._cleanup_errors)
    return list(job._cleanup_errors)


def reconcile_owned(*, runner=None):
    """Retire retained/abandoned creations belonging to this controller only."""
    failures = []
    for job in tuple(_LIVE_JOBS):
        record = _CREATIONS[job]
        if record.owner != os.getpid() or (runner is not None and record.runner is not runner):
            continue
        try:
            _retire_unit(job, deadline=time.monotonic()+5)
        except (SignalRefusal, subprocess.TimeoutExpired, OSError) as exc:
            reason = getattr(exc, 'code', type(exc).__name__)
            job._cleanup_errors.append(reason)
            _creation_evidence(job, 'cleanup_pending')
            failures.append({'unit': record.unit, 'reason': reason})
    return failures


def _exit_evidence(state):
    """A zero main exit and Result=success are both required for job success."""
    main = None
    try:
        code, status = int(state['ExecMainCode']), int(state['ExecMainStatus'])
        if code == 1 and status >= 0:
            main = status
        elif code in (2, 3) and status > 0:
            main = -status
    except (KeyError, ValueError):
        pass
    result = state.get('Result', '')
    peak = state.get('MemoryPeak', '')
    peak = int(peak) if peak.isascii() and peak.isdecimal() else None
    if not result or (result == 'success' and main is None):
        return None, 'job_result_unproven'
    if result == 'success':
        reason = None if main == 0 else 'job_result_inconsistent'
    else:
        known = ('oom-kill', 'resources', 'timeout', 'signal', 'core-dump', 'exit-code',
                 'protocol', 'watchdog', 'start-limit-hit', 'exec-condition')
        reason = 'job_'+result.replace('-', '_') if result in known else 'job_service_failure'
    rc = main if main not in (None, 0) else (0 if result == 'success' else 5)
    return JobResult(rc, result, peak, main, reason), None


def _inherited_limits():
    try:
        root = Path('/sys/fs/cgroup')
        relative = Path('/proc/self/cgroup').read_text().split('0::', 1)[1].splitlines()[0]
        leaf = (root / relative.lstrip('/')).resolve()
        leaf.relative_to(root)
        limits = {key: [] for key in ('memory.high', 'memory.max', 'memory.swap.max')}
        for group in (leaf, *leaf.parents):
            if group == root:
                break
            for key in limits:
                path = group / key
                if not path.exists():
                    continue
                value = path.read_text().strip()
                if value != 'max':
                    if not value.isascii() or not value.isdecimal():
                        raise ValueError
                    limits[key].append(int(value))
        if not all(limits.values()):
            raise ValueError
        return MemoryLimits(*(min(values) for values in limits.values()))
    except (OSError, ValueError, IndexError) as exc:
        raise SignalRefusal('memory_limits_unproven') from exc


def _command(argv, declared):
    """Translate the callers' memory scope into service properties, once."""
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(a, (str, os.PathLike)) for a in argv):
        raise SignalRefusal('group_spawn_unproven')
    argv = [os.fspath(a) for a in argv]
    if Path(argv[0]).name == 'systemd-run':
        properties, index = {}, 1
        while index < len(argv):
            arg = argv[index]
            if arg in ('--user', '--scope', '-q', '--quiet'):
                index += 1
            elif arg in ('-p', '--property') and index+1 < len(argv):
                key, sep, value = argv[index+1].partition('=')
                if not sep or key not in ('MemoryHigh', 'MemoryMax', 'MemorySwapMax'):
                    raise SignalRefusal('memory_limits_unproven')
                properties[key] = value
                index += 2
            elif arg == '--':
                index += 1
                break
            else:
                break
        def amount(value):
            match = re.fullmatch(r'([0-9]+)([KMGT]?)', value)
            if match is None:
                raise SignalRefusal('memory_limits_unproven')
            return int(match[1]) * 1024**('KMGT'.index(match[2])+1 if match[2] else 0)
        try:
            scoped = MemoryLimits(*(amount(properties[k]) for k in ('MemoryHigh', 'MemoryMax', 'MemorySwapMax')))
        except KeyError as exc:
            raise SignalRefusal('memory_limits_unproven') from exc
        if declared is not None:
            scoped = MemoryLimits(min(scoped.high, declared.high), min(scoped.maximum, declared.maximum), 0)
        declared, argv = scoped, argv[index:]
    if not argv:
        raise SignalRefusal('group_spawn_unproven')
    return argv, declared


def _output(job, stream, name):
    if stream is None or stream == subprocess.PIPE:
        path = Path(job._scratch.name) / name
        path.touch()
        return path
    if stream == subprocess.DEVNULL:
        return Path(os.devnull)
    try:
        # Duplicate only a data file, never a process capability. The caller may
        # close its stream without rebinding the service's output descriptor.
        handle = os.fdopen(os.dup(stream.fileno()), 'ab', buffering=0)
        job._files.append(handle)
        return Path(f'/proc/{os.getpid()}/fd/{handle.fileno()}')
    except (AttributeError, OSError, ValueError) as exc:
        raise SignalRefusal('job_stream_invalid') from exc


def popen(argv, *, start_new_session=False, runner=None, memory_limits=None, cwd=None,
          env=None, stdin=None, stdout=None, stderr=None, input=None, text=False,
          encoding=None, runtime_s=3600, **kwargs):
    """Compatibility entry point; returns a Job, never a subprocess handle.

    Input is a byte snapshot, supplied before launch. Arbitrary inherited FDs,
    shell/preexec callbacks, and multiprocessing object upgrades are refused.
    """
    if start_new_session is not True:
        raise SignalRefusal('group_session_required')
    if any(k not in ('close_fds', 'bufsize', 'errors', 'universal_newlines') for k in kwargs):
        raise SignalRefusal('group_spawn_unproven')
    if kwargs.get('close_fds', True) is not True or type(runtime_s) is not int or not 1 <= runtime_s <= 86400:
        raise SignalRefusal('group_spawn_unproven')
    argv, memory_limits = _command(argv, memory_limits)
    if memory_limits is not None and type(memory_limits) is not MemoryLimits:
        raise SignalRefusal('memory_limits_unproven')
    # A local runner injection explicitly selects the systemd contract for its
    # unit tests. Environment selection is otherwise exact, never a fallback.
    runner = runner or _RUNNER.get()
    backend = 'systemd' if runner is not None else os.environ.get('AF_PROC_BACKEND', 'systemd')
    if backend not in ('systemd', 'pidns'):
        raise SignalRefusal('proc_backend_invalid')
    if backend == 'pidns':
        from asset_factory import proc_pidns
        proc_pidns.require_namespace()
    else:
        runner = runner or SystemdRunner()
        available = runner.run(['systemctl', '--user', 'show', '--property=Version', '--value'], timeout=5)
        if available.returncode != 0 or not available.stdout.strip():
            raise SignalRefusal('systemd_user_manager_unavailable')
    memory_limits = memory_limits or _inherited_limits()
    job = Job('af-job-'+uuid.uuid4().hex+'.service', runner, seal=_SEAL)
    job._argv, job._text, job._encoding = argv, bool(text), encoding or 'utf-8'
    job._scratch = _OwnedScratch(prefix='af-job-')
    job._stdout_path = _output(job, stdout, 'stdout')
    job._stderr_path = job.stdout_path if stderr == subprocess.STDOUT else _output(job, stderr, 'stderr')
    source = Path(job._scratch.name) / 'stdin'
    if input is not None:
        data = input.encode(job._encoding) if isinstance(input, str) else bytes(input)
    elif stdin not in (None, subprocess.PIPE, subprocess.DEVNULL):
        data = stdin.read()
        if isinstance(data, str): data = data.encode(job._encoding)
    else:
        data = b''
    source.write_bytes(data)
    environment = dict(os.environ if env is None else env)
    if any(type(k) is not str or type(v) is not str or '=' in k or '\x00' in k+v for k, v in environment.items()):
        raise SignalRefusal('group_spawn_unproven')
    executable = (argv[0] if Path(argv[0]).is_absolute() and os.access(argv[0], os.X_OK)
                  else shutil.which(argv[0], path=environment.get('PATH', os.defpath)))
    if executable is None:
        raise FileNotFoundError(argv[0])
    if backend == 'pidns':
        job._backend = proc_pidns.launch(job, [executable, *argv[1:]],
            cwd=cwd, env=environment, source=source, runtime_s=runtime_s)
        return job
    env_binary = shutil.which('env', path=os.defpath)
    if env_binary is None:
        raise SignalRefusal('group_spawn_unproven')
    properties = {'MemoryHigh': memory_limits.high, 'MemoryMax': memory_limits.maximum,
        'MemorySwapMax': memory_limits.swap, 'KillMode': 'control-group', 'SendSIGKILL': 'yes',
        'TimeoutStopSec': '2s', 'RuntimeMaxSec': f'{runtime_s}s', 'RemainAfterExit': 'yes',
        'CPUAffinity': ' '.join(str(c) for c in sorted(os.sched_getaffinity(0))), 'Nice': os.nice(0),
        'CollectMode': 'inactive', 'StandardInput': f'file:{source}',
        'StandardOutput': f'append:{job.stdout_path}',
        'StandardError': 'inherit' if stderr == subprocess.STDOUT else f'append:{job.stderr_path}'}
    command = ['systemd-run', '--user', '--no-block', '--quiet', '--service-type=exec',
        '--expand-environment=no', '--unit='+job.unit_name, '--working-directory='+str(Path(cwd or Path.cwd()).resolve())]
    command += [f'--property={k}={v}' for k, v in properties.items()]
    command += ['--', env_binary, '-i', *[f'{k}={v}' for k, v in sorted(environment.items())], executable, *argv[1:]]
    # UUID names are generated locally before any start request. Reject a
    # visible collision before claiming authority; external same-name creation
    # during this transaction is P8-NAME, explicitly OUT_OF_CONTRACT.
    existing = _unit_call(runner, 'show', job.unit_name, '--property=LoadState')
    if existing.returncode != 0 or 'LoadState=not-found' not in existing.stdout.splitlines():
        raise SignalRefusal('group_spawn_unproven')
    _CREATIONS[job] = _Creation(job.unit_name, runner, os.getpid(), True)
    _LIVE_JOBS.add(job)
    try:
        _creation_evidence(job, 'start_pending')
        reply = runner.run(command, timeout=5)
        if reply.returncode != 0:
            raise SignalRefusal('group_spawn_unproven')
        _creation_evidence(job, 'started')
    except Exception as exc:
        failure = exc if isinstance(exc, SignalRefusal) else SignalRefusal('systemd_user_manager_unavailable')
        try:
            _retire_unit(job, deadline=time.monotonic()+5)
        except Exception as cleanup_error:
            job._cleanup_errors.append(getattr(cleanup_error, 'code', type(cleanup_error).__name__))
            try:
                _creation_evidence(job, 'cleanup_pending')
            except Exception as evidence_error:
                job._cleanup_errors.append(type(evidence_error).__name__)
        failure.unit_name = job.unit_name
        failure.creation_path = str(Path(job._scratch.name)/'creation.json')
        failure.cleanup_errors = list(job._cleanup_errors)
        raise failure
    return job


def communicate(job, input=None, timeout=None):
    _owned(job)
    if input is not None:
        raise SignalRefusal('job_input_after_start')
    job.wait(timeout)
    output = tuple(path.read_bytes() for path in (job.stdout_path, job.stderr_path))
    return tuple(data.decode(job._encoding) for data in output) if job._text else output


def wait_exit(child, timeout=None): return _owned(child).wait(timeout)


def signal_group(job, sig):
    _owned(job)
    if type(sig) is int and sig == 0:
        if poll_exit(job) is not None:
            raise ProcessLookupError('job_finished')
        return None
    return job.stop(sig)


def signal_pid(job, sig): return signal_group(job, sig)


def register_group(child, *, start_new_session):
    if start_new_session is not True:
        raise SignalRefusal('group_session_required')
    return _owned(child)


def register_pid(child): return _owned(child)


def register_child(value): raise SignalRefusal('pid_spawn_unproven')

def start_process(value): raise SignalRefusal('group_spawn_unproven')

def reap_child(value, **kwargs): raise SignalRefusal('pid_spawn_unproven')

def forget_child(job): return _owned(job).stop()
