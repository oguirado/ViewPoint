# @module {"id":"wa.learn.mw_live_evidence","layer":4,"owner":"LEARN","deps":["wa.learn.demonstrations","wa.learn.mw_locate_ai","wa.learn.mw_owners_srf","wa.learn.mw_workflow","wa.perceive.srf_identity","wa.perceive.vision_bridge","wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Trusted live evidence. Patterns: mw_locate_ai bounded pipes/RAM validation,
vision_bridge C2 OCR command, demonstrations PNG decoder, mw_owners_srf custody.
Configuration and export authority come exclusively from the embedding caller.
"""
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
import shutil
from pathlib import Path
import resource
import signal
import selectors
import stat
import subprocess
import tempfile
import time
import uuid

from wender.learn.demonstrations import DemoLimits, _png
from wender.learn.mw_locate_ai import (LocatePolicy, LocateEvidence, LocateAccounting,
    LocateAccountingSnapshot, MuseLocateTransport, _policy, _ram_path)
from wender.learn.mw_owners_srf import open_srf_owners_with_locator
from wender.learn.mw_workflow import ObservationValue, GroundedTarget, RunResult, LocationFailure, run_workflow
from wender.perceive.srf_identity import SrfIdentitySurface
from wender.perceive.vision_bridge import (SubprocessVisionEngine, DescribeResult, Mark,
                                         _parse_description)
from wender.scripts.types import AdmissionCheck, Truth, Reason

_YES = AdmissionCheck(Truth.TRUE, None)
_NO = AdmissionCheck(Truth.FALSE, Reason.UNKNOWN)
_FIELDS = {'rung', 'mark_kind', 'ocr_label', 'locale', 'rel_box_bps', 'capability_id'}


def _need(ok, gate):
    if not ok:
        raise ValueError('LIVE_EVIDENCE:' + gate)


def _yes(value):
    return type(value) is AdmissionCheck and value.result is Truth.TRUE and value.reason is None


def _call(callback, **kwargs):
    try:
        result = callback(**kwargs)
        return result if _yes(result) else _NO
    except Exception:
        return _NO


def _fingerprint(target):
    _need(type(target) is dict and set(target) == _FIELDS, 'target_schema')
    _need(target['rung'] == 'T2' and all(type(target[k]) is str and 0 < len(target[k]) <= n
          for k, n in (('mark_kind', 400), ('ocr_label', 400), ('locale', 80), ('capability_id', 80))), 'target_schema')
    box = target['rel_box_bps']
    _need(type(box) is list and len(box) == 4 and all(type(x) is int and 0 <= x <= 10000 for x in box)
          and box[0] < box[2] and box[1] < box[3], 'target_schema')
    return json.dumps(target, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


# @anchor:wa.learn.mw_live_evidence.LiveEvidencePolicy
@dataclass(frozen=True)
class LiveEvidencePolicy:
    decode_limits: DemoLimits
    ve_stdout_bytes: int
    ve_stderr_bytes: int
    ve_process_bytes: int
    metadata_bytes: int
    ocr_tasks_max: int = 32
    ocr_control_ns: int = 10_000_000_000  # [TARGET, BUILD_ARCH wave-28 control budget]


# @anchor:wa.learn.mw_live_evidence.LiveEvidenceTrust
@dataclass(frozen=True)
class LiveEvidenceTrust:
    capability_revision: str
    supported_locales: tuple[str, ...]
    destination: object = field(repr=False)
    export_check: object = field(repr=False)
    custody_check: object = field(repr=False)
    support_check: object = field(repr=False)
    forbidden_substrings: tuple[str, ...] = field(repr=False)


# @anchor:wa.learn.mw_live_evidence.FrameBinding
@dataclass(frozen=True, repr=False)
class FrameBinding:
    scope: object
    authority: object
    run: object
    step: object
    surface: object
    window: str
    sequence: int
    dimensions: tuple
    capture_interval: tuple
    source_identity: tuple
    encoded_digest: str
    rgb_digest: str
    image_identities: tuple


_FALLBACKS = ('runner_absent', 'manager_unavailable', 'scope_unavailable')


# @anchor:wa.learn.mw_live_evidence.OCRExecutionRecord
@dataclass(frozen=True)
class OCRExecutionRecord:
    mode: str
    fallback_reason: str | None
    tasks_max: int | None
    memory_max_bytes: int | None

    # @anchor:wa.learn.mw_live_evidence.OCRExecutionRecord.__post_init__
    def __post_init__(self):
        _need((self.mode == 'systemd_scope' and self.fallback_reason is None
               and all(type(v) is int and v > 0 for v in (self.tasks_max, self.memory_max_bytes)))
              or (self.mode == 'direct' and self.fallback_reason in _FALLBACKS
                  and self.tasks_max is None and self.memory_max_bytes is None), 'ocr_record')


# @anchor:wa.learn.mw_live_evidence.OCRExecutionSummary
@dataclass(frozen=True)
class OCRExecutionSummary:
    scope_attempts: int
    direct_attempts: int
    fallback_counts: tuple[tuple[str, int], ...]


# @anchor:wa.learn.mw_live_evidence.LiveWorkflowResult
@dataclass(frozen=True)
class LiveWorkflowResult:
    result: RunResult
    accounting: LocateAccountingSnapshot
    ocr_execution: OCRExecutionSummary | None = None


@dataclass(repr=False)
class _Allocation:
    step: object
    fingerprint: str
    paths: list = field(default_factory=list)
    owned: dict = field(default_factory=dict)
    evidence: object = None
    grounded: dict = field(default_factory=dict)
    released: bool = False


def _identity(meta):
    return (meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)


def _asset(path, workspace, cap):
    path = _ram_path(path, workspace, cap)
    _need(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(fd)
        _need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
              and not before.st_mode & 0o077 and before.st_size <= cap, 'regular_file')
        data = bytearray()
        while True:
            chunk = os.read(fd, min(65536, cap + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            _need(len(data) <= cap, 'image_bound')
        _need(_identity(before) == _identity(os.fstat(fd)) == _identity(path.stat()), 'file_identity')
        return bytes(data), _identity(before)
    finally:
        os.close(fd)


# @anchor:wa.learn.mw_live_evidence._ocr_child_env
def _ocr_child_env():
    """Minimal child env plus the OCR toolchain location: the tesseract binary dir and TESSDATA_PREFIX.
    A user-local install (~/.local/bin, ~/.local/share/tessdata) is otherwise invisible to the child
    [MEASURED, W25 real-OCR smoke: every mark tessdata_absent]."""
    path = [os.defpath]
    binary = shutil.which('tesseract')
    if binary:
        path.insert(0, str(Path(binary).resolve().parent))
    env = {'PATH': os.pathsep.join(path), 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'}
    prefix = os.environ.get('TESSDATA_PREFIX', '').strip()
    if not prefix:
        for candidate in (Path.home() / '.local/share/tessdata', Path('/usr/share/tesseract-ocr/5/tessdata')):
            if (candidate / 'eng.traineddata').is_file():
                prefix = str(candidate)
                break
    if prefix:
        env['TESSDATA_PREFIX'] = prefix
    return env


# @anchor:wa.learn.mw_live_evidence._ocr_metadata
def _ocr_metadata(policy, attempts, counts):
    payload = {'mode': 'systemd_scope', 'fallback_reason': 'manager_unavailable',
        'tasks_max': policy.ocr_tasks_max, 'memory_max_bytes': policy.ve_process_bytes,
        'scope_attempts': attempts[0], 'direct_attempts': attempts[1],
        'fallback_counts': tuple(zip(_FALLBACKS, counts))}
    size = len(json.dumps(payload, separators=(',', ':')).encode('ascii'))
    _need(size <= policy.metadata_bytes, 'ocr_metadata')
    return size



# @anchor:wa.learn.mw_live_evidence._ocr_read
def _ocr_read(child, *, policy, locator_policy, clock, deadline_ns):
    buffers = [bytearray(), bytearray()]
    caps = (policy.ve_stdout_bytes, policy.ve_stderr_bytes)
    with selectors.DefaultSelector() as selector:
        for index, pipe in enumerate((child.stdout, child.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, index)
        while selector.get_map():
            remaining = deadline_ns - clock()
            _need(remaining > 0, 'ocr_deadline')
            for key, _ in selector.select(min(locator_policy.poll_ns, remaining) / 1_000_000_000):
                i = key.data
                chunk = os.read(key.fd, min(65536, caps[i] + 1 - len(buffers[i])))
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    _need(len(buffers[i]) + len(chunk) <= caps[i], 'ocr_output_bound')
                    buffers[i].extend(chunk)
    _need(clock() < deadline_ns, 'ocr_deadline')
    child.wait(timeout=max(0, deadline_ns - clock()) / 1_000_000_000)
    return tuple(bytes(b) for b in buffers)


# @anchor:wa.learn.mw_live_evidence._ocr_group_join
def _ocr_group_join(child, *, clock, deadline_ns):
    if child is None:
        return
    # Guard: a non-int (mocked) or <= 1 pid makes killpg act on every process (kill(-1)); never signal our own group.
    _need(type(child.pid) is int and child.pid > 1 and child.pid != os.getpgid(0), 'ocr_group')
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _need(clock() < deadline_ns, 'ocr_deadline')
    child.wait(timeout=max(0, deadline_ns - clock()) / 1_000_000_000)
    try:
        os.killpg(child.pid, 0)
    except ProcessLookupError:
        return
    raise ValueError('LIVE_EVIDENCE:ocr_group')


# @anchor:wa.learn.mw_live_evidence._OCRScopeRunner
class _OCRScopeRunner:
    # @anchor:wa.learn.mw_live_evidence._OCRScopeRunner.__init__
    def __init__(self):
        self.runner = self.control = None
        self.pending = self.helper = None
        self.policy = None
        self.env = None

    def _limits(self):
        # [TARGET] Partition the existing allowance among probe tasks, launcher and one control helper.
        allowance = self.policy.ve_process_bytes // (self.policy.ocr_tasks_max + 2)
        _need(allowance > 0, 'ocr_control_budget')
        resource.setrlimit(resource.RLIMIT_AS, (allowance,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    def _command(self, command, *, locator_policy, clock, deadline_ns):
        _need(self.helper is None and clock() < deadline_ns, 'ocr_control')
        self.helper = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=self.env, start_new_session=True, preexec_fn=self._limits)
        try:
            out, _ = _ocr_read(self.helper, policy=self.policy, locator_policy=locator_policy,
                               clock=clock, deadline_ns=deadline_ns)
            return self.helper.returncode, out
        finally:
            _ocr_group_join(self.helper, clock=clock, deadline_ns=deadline_ns)
            self.helper.stdout.close()
            self.helper.stderr.close()
            self.helper = None

    def _show(self, unit, **kwargs):
        rc, out = self._command([self.control, '--user', 'show', unit,
            '--property=LoadState,ActiveState,ControlGroup,TasksCurrent,TasksMax,MemoryMax,Slice'], **kwargs)
        _need(rc == 0, 'ocr_scope_query')
        rows = out.decode('ascii').splitlines()
        _need(rows and all('=' in r for r in rows), 'ocr_scope_query')
        pairs = [r.split('=', 1) for r in rows]
        data = dict(pairs)
        _need(len(data) == len(pairs) and set(data) == {
            'LoadState', 'ActiveState', 'ControlGroup', 'TasksCurrent', 'TasksMax', 'MemoryMax', 'Slice'},
            'ocr_scope_query')
        _need(data['LoadState'] in ('loaded', 'not-found')
              and data['ActiveState'] in ('active', 'inactive', 'failed', 'activating', 'deactivating')
              and (not data['ControlGroup'] or (data['ControlGroup'].startswith('/')
                   and '..' not in data['ControlGroup'].split('/')))
              and all(data[k].isascii() and (data[k].isdigit() or data[k] in ('infinity', '[not set]'))
                      for k in ('TasksCurrent', 'TasksMax', 'MemoryMax'))
              and (data['LoadState'] != 'loaded' or data['TasksCurrent'].isdigit())
              and (not data['Slice'] or (data['Slice'].endswith('.slice') and '/' not in data['Slice'])),
              'ocr_scope_query')
        return data

    # @anchor:wa.learn.mw_live_evidence._OCRScopeRunner.argv
    def argv(self, command, *, unit):
        _need(type(unit) is str and unit.startswith('memoryweb-ocr-') and unit.endswith('.scope')
              and len(unit) == len('memoryweb-ocr-') + 32 + len('.scope')
              and all(c in '0123456789abcdef' for c in unit[len('memoryweb-ocr-'):-6]), 'ocr_unit')
        return [self.runner, '--user', '--scope', '--quiet', '--collect', '--no-ask-password',
            '--slice-inherit', '--unit=' + unit, '--description=MemoryWeb-OCR',
            '--property=TasksMax=' + str(self.policy.ocr_tasks_max),
            '--property=MemoryMax=' + str(self.policy.ve_process_bytes), '--', *command]

    def _empty(self, unit, **kwargs):
        data = self._show(unit, **kwargs)
        if data['LoadState'] == 'not-found':
            _need(data['ActiveState'] == 'inactive' and data['ControlGroup'] == '', 'ocr_scope_query')
            return True
        _need(data['LoadState'] == 'loaded', 'ocr_scope_query')
        if data['TasksCurrent'] == '0' and data['ActiveState'] in ('inactive', 'failed'):
            _need(not data['ControlGroup'], 'ocr_scope_query')
            return True
        return False

    # @anchor:wa.learn.mw_live_evidence._OCRScopeRunner.drain
    def drain(self, *, unit, child, locator_policy, clock, deadline_ns):
        try:
            if self.helper is not None:
                _ocr_group_join(self.helper, clock=clock, deadline_ns=deadline_ns)
                self.helper.stdout.close()
                self.helper.stderr.close()
                self.helper = None
            _ocr_group_join(child, clock=clock, deadline_ns=deadline_ns)
            kw = dict(locator_policy=locator_policy, clock=clock, deadline_ns=deadline_ns)
            if self._empty(unit, **kw):
                return _YES
            for args in (['kill', '--kill-whom=all', '--signal=KILL', unit], ['stop', unit]):
                try:
                    rc, _ = self._command([self.control, '--user', *args], **kw)
                except Exception:
                    # A control failure is recoverable only after its helper was joined.
                    if self.helper is not None or clock() >= deadline_ns:
                        return _NO
                    return _YES if self._empty(unit, **kw) else _NO
                if rc != 0:
                    return _YES if self._empty(unit, **kw) else _NO
                if args[0] == 'kill' and self._empty(unit, **kw):
                    return _YES
            _need(self._empty(unit, **kw), 'ocr_scope_alive')
            return _YES
        except Exception:
            return _NO

    # @anchor:wa.learn.mw_live_evidence._OCRScopeRunner.prepare
    def prepare(self, *, policy, locator_policy, clock, deadline_ns):
        self.policy = policy
        control_deadline = min(deadline_ns, clock() + policy.ocr_control_ns)
        kw = dict(locator_policy=locator_policy, clock=clock, deadline_ns=control_deadline)
        _need(clock() < deadline_ns and self.pending is None and self.helper is None, 'ocr_scope_state')
        self.runner, self.control = shutil.which('systemd-run'), shutil.which('systemctl')
        sleeper = shutil.which('sleep')
        if not all((self.runner, self.control, sleeper)):
            return 'runner_absent'
        self.runner = str(Path(self.runner).resolve())
        self.control = str(Path(self.control).resolve())
        sleeper = str(Path(sleeper).resolve())
        self.env = _ocr_child_env()
        runtime = Path('/run/user') / str(os.getuid())
        if runtime.is_dir():
            meta = runtime.stat()
            _need(meta.st_uid == os.getuid() and not meta.st_mode & 0o077 and not runtime.is_symlink(),
                  'ocr_manager_environment')
            self.env['XDG_RUNTIME_DIR'] = str(runtime)
        rc, out = self._command([self.control, '--user', 'show', '--property=Version'], **kw)
        if rc != 0:
            return 'manager_unavailable'
        _need(out.startswith(b'Version=') and len(out.splitlines()) == 1
              and bool(out[len(b'Version='):].strip()), 'ocr_manager_probe')
        unit = 'memoryweb-ocr-' + uuid.uuid4().hex + '.scope'
        # Ownership begins before Popen: an exception may follow manager admission.
        self.pending = (unit, None)
        available = False
        try:
            child = subprocess.Popen(self.argv([sleeper, str(max(1, (control_deadline - clock()) // 1_000_000_000))], unit=unit),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
                start_new_session=True, preexec_fn=self._limits)
            self.pending = (unit, child)
            # No private data enters the harmless probe. Poll a bounded control response until admission.
            while clock() < control_deadline:
                data = self._show(unit, **kw)
                if data['LoadState'] == 'loaded' and data['ActiveState'] == 'active':
                    cg = data['ControlGroup']
                    _need(cg.startswith('/') and '..' not in cg.split('/'), 'ocr_controller')
                    root = Path('/sys/fs/cgroup') / cg.lstrip('/')
                    own = Path('/proc/self/cgroup').read_text().splitlines()
                    slices = [part for row in own for part in row.split('/') if part.endswith('.slice')]
                    _need(bool(slices), 'ocr_slice')
                    available = (data['TasksMax'] == str(policy.ocr_tasks_max)
                        and data['MemoryMax'] == str(policy.ve_process_bytes) and data['Slice'] == slices[-1]
                        and (root / 'pids.max').read_text().strip() == str(policy.ocr_tasks_max)
                        and (root / 'memory.max').read_text().strip() == str(policy.ve_process_bytes))
                    break
                if child.poll() is not None:
                    # Read bounded diagnostics solely to enforce caps, never retain them.
                    _ocr_read(child, policy=policy, **kw)
                    break
            _need(clock() < control_deadline, 'ocr_deadline')
        except Exception:
            available = False
        finally:
            cleanup_deadline = clock() + policy.ocr_control_ns
            _need(_yes(self.drain(unit=unit, child=self.pending[1], locator_policy=locator_policy,
                clock=clock, deadline_ns=cleanup_deadline)), 'ocr_join')
            probe = self.pending[1]
            # Joined processes and the empty-unit proof end live ownership, even if diagnostics fail.
            self.pending = None
            if probe is not None:
                try:
                    _ocr_read(probe, policy=policy, locator_policy=locator_policy,
                              clock=clock, deadline_ns=cleanup_deadline)
                except Exception:
                    available = False
                finally:
                    probe.stdout.close()
                    probe.stderr.close()
        return None if available else 'scope_unavailable'

    def _retry_probe(self, *, locator_policy, clock, deadline_ns):
        if self.pending is None:
            if self.helper is not None:
                _ocr_group_join(self.helper, clock=clock, deadline_ns=deadline_ns)
                self.helper.stdout.close()
                self.helper.stderr.close()
                self.helper = None
            return _YES
        unit, child = self.pending
        result = self.drain(unit=unit, child=child, locator_policy=locator_policy,
                            clock=clock, deadline_ns=deadline_ns)
        if _yes(result):
            if child is not None:
                child.stdout.close()
                child.stderr.close()
            self.pending = None
        return result



# @anchor:wa.learn.mw_live_evidence.LiveOCRDescriber
class LiveOCRDescriber:
    # @anchor:wa.learn.mw_live_evidence.LiveOCRDescriber.__init__
    def __init__(self, *, evidence_policy, locator_policy, clock, ve_root=None, scope_runner=None):
        _policy(locator_policy)
        _need(type(evidence_policy) is LiveEvidencePolicy and
              all(type(getattr(evidence_policy, name)) is int and getattr(evidence_policy, name) > 0
                  for name in ('ve_stdout_bytes', 've_stderr_bytes', 've_process_bytes', 'metadata_bytes', 'ocr_tasks_max', 'ocr_control_ns')), 'ocr_configuration')
        self.engine = SubprocessVisionEngine(ve_root)
        self.policy, self.locator, self.clock = evidence_policy, locator_policy, clock
        self.child = None
        self.scope_runner = _OCRScopeRunner() if scope_runner is None else scope_runner
        self.pending_scope = None
        self.poisoned = False
        self.last_execution = None
        self._attempts = [0, 0]
        self._fallback_counts = [0] * len(_FALLBACKS)
        _ocr_metadata(self.policy, self._attempts, self._fallback_counts)

    # @anchor:wa.learn.mw_live_evidence.LiveOCRDescriber.execution_summary
    def execution_summary(self):
        return OCRExecutionSummary(*self._attempts, tuple(zip(_FALLBACKS, self._fallback_counts)))

    # @anchor:wa.learn.mw_live_evidence.LiveOCRDescriber.drain
    def drain(self):
        if self.child is None and self.pending_scope is None and not self.poisoned:
            return _YES
        deadline = self.clock() + self.policy.ocr_control_ns
        try:
            if self.pending_scope is not None:
                _need(_yes(self.scope_runner.drain(unit=self.pending_scope, child=self.child,
                    locator_policy=self.locator, clock=self.clock, deadline_ns=deadline)), 'ocr_join')
            else:
                _ocr_group_join(self.child, clock=self.clock, deadline_ns=deadline)
            if type(self.scope_runner) is _OCRScopeRunner:
                _need(_yes(self.scope_runner._retry_probe(locator_policy=self.locator,
                    clock=self.clock, deadline_ns=deadline)), 'ocr_join')
            if self.child is not None:
                for pipe in (self.child.stdout, self.child.stderr):
                    pipe.close()
            self.child = None
            self.pending_scope = None
            self.poisoned = False
            return _YES
        except Exception:
            self.poisoned = True
            return _NO

    # @anchor:wa.learn.mw_live_evidence.LiveOCRDescriber.describe
    def describe(self, frame, *, marks_out, ocr_marks, deadline_ns):
        _need(ocr_marks is True and self.child is None and self.pending_scope is None and not self.poisoned, 'ocr_state')
        _need(type(deadline_ns) is int and self.clock() < deadline_ns, 'ocr_deadline')
        frame, marks_out = Path(frame), Path(marks_out)
        _need(frame.parent == marks_out.parent and frame.parent.is_relative_to('/dev/shm/vqa-cu'), 'ocr_workspace')
        _ram_path(frame, frame.parent, self.locator.image_bytes)
        _ram_path(marks_out, frame.parent, self.locator.image_bytes)
        p = self.policy
        _need(os.getuid() != 0, 'ocr_child_enforcement')
        def limits():
            resource.setrlimit(resource.RLIMIT_FSIZE, (self.locator.image_bytes,) * 2)
            resource.setrlimit(resource.RLIMIT_AS, (p.ve_process_bytes,) * 2)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            # The OCR child must spawn tesseract, so RLIMIT_NPROC is NOT zeroed (judge W25 fix 1).
            # The tree stays bounded by start_new_session + killpg drain, RLIMIT_AS / FSIZE / CORE,
            # the stdout/stderr caps and the deadline.
        try:
            _ocr_metadata(p, [n + 1 for n in self._attempts], [n + 1 for n in self._fallback_counts])
            self.poisoned = True
            reason = self.scope_runner.prepare(policy=p, locator_policy=self.locator,
                                              clock=self.clock, deadline_ns=deadline_ns)
            self.poisoned = False
            _need(reason is None or reason in _FALLBACKS, 'ocr_availability')
            _need(self.clock() < deadline_ns, 'ocr_deadline')
            command = [self.engine.python, '-m', 'vadapt.cu_describe',
                '--frame', str(frame), '--marks-out', str(marks_out), '--ocr-marks', '--json']
            env = _ocr_child_env()
            if reason is None:
                unit = 'memoryweb-ocr-' + uuid.uuid4().hex + '.scope'
                command = self.scope_runner.argv(command, unit=unit)
                self.last_execution = OCRExecutionRecord('systemd_scope', None, p.ocr_tasks_max, p.ve_process_bytes)
                self._attempts[0] += 1
                self.pending_scope = unit
                if type(self.scope_runner) is _OCRScopeRunner:
                    env = self.scope_runner.env
            else:
                self.last_execution = OCRExecutionRecord('direct', reason, None, None)
                self._attempts[1] += 1
                self._fallback_counts[_FALLBACKS.index(reason)] += 1
            self.child = subprocess.Popen(command,
                cwd=self.engine.ve_root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, preexec_fn=limits, start_new_session=True, env=env)
            buffers = _ocr_read(self.child, policy=p, locator_policy=self.locator,
                                clock=self.clock, deadline_ns=deadline_ns)
            _need(self.child.returncode == 0, 'ocr_exit')
            _need(self.clock() < deadline_ns, 'ocr_deadline')
            result = _parse_description(bytes(buffers[0]), ocr_requested=True)
            _need(result.marks_png == str(marks_out), 'marks_path')
            _need(self.clock() < deadline_ns, 'ocr_deadline')
            return result
        except Exception:
            raise ValueError('LIVE_EVIDENCE:ocr_refused') from None
        finally:
            # A failed probe cleanup already spent this operation's shared cleanup budget.
            # Preserve it for an explicit later drain retry instead of restarting the clock.
            if self.poisoned and type(self.scope_runner) is _OCRScopeRunner and self.scope_runner.pending is not None:
                _need(False, 'ocr_join')
            _need(_yes(self.drain()), 'ocr_join')


# @anchor:wa.learn.mw_live_evidence.LiveEvidenceOwner
class LiveEvidenceOwner:
    # @anchor:wa.learn.mw_live_evidence.LiveEvidenceOwner.__init__
    def __init__(self, *, ports, locator_policy, evidence_policy, trust, workspace, surface_slot, describer):
        self.ports, self.policy, self.config, self.trust = ports, locator_policy, evidence_policy, trust
        self.workspace, self.surface_slot, self.describer = Path(workspace), surface_slot, describer
        self.scope, self.authority = ports.scope, ports.authority
        self.run_deadline_ns = ports.limits.deadline_ns
        self.capability_owner = None
        self.allocations, self.primary = {}, {}
        self.run = None
        self.sequence = None
        self.partial = None
        self._configuration()
        _need(type(surface_slot) is dict and
              (describer is None or callable(getattr(describer, 'describe', None))), 'dependencies')

    def _configuration(self):
        p, c, t, limits = self.policy, self.config, self.trust, self.ports.limits
        _policy(p)
        _need(type(c) is LiveEvidencePolicy and type(c.decode_limits) is DemoLimits and
              all(type(v) is int and v > 0 for k, v in vars(c).items() if k != 'decode_limits') and
              all(type(v) is int and v > 0 for v in vars(c.decode_limits).values()), 'configuration')
        _need(type(t) is LiveEvidenceTrust and type(t.capability_revision) is str and bool(t.capability_revision)
              and type(t.supported_locales) is tuple and bool(t.supported_locales)
              and all(type(v) is str and bool(v) for v in t.supported_locales)
              and type(t.forbidden_substrings) is tuple and all(type(v) is str and bool(v) for v in t.forbidden_substrings)
              and all(callable(v) for v in (t.export_check, t.custody_check, t.support_check)), 'trust')
        retained = 2 * p.image_bytes + c.metadata_bytes
        extra = 8 * p.image_bytes + c.decode_limits.memory_bytes + c.ve_process_bytes + c.ve_stdout_bytes + c.ve_stderr_bytes + c.metadata_bytes
        _need(p.retained_bytes >= retained and p.max_calls * p.retained_bytes <= limits.retained_bytes
              and p.peak_bytes + extra + (p.max_calls - 1) * p.retained_bytes <= limits.peak_bytes
              and c.decode_limits.png_bytes <= p.image_bytes
              and p.max_calls <= min(limits.acquisitions, self.ports.native_read.policy.publications), 'budgets')
        _need(self.workspace.is_relative_to('/dev/shm/vqa-cu'), 'workspace_root')
        _ram_path(self.workspace, self.workspace)
        _need(self.workspace.is_dir() and not any(x.is_symlink() for x in (self.workspace, *self.workspace.parents)), 'workspace')

    def _safe(self, step):
        _need(self.run is step.run and step.run.current_step is step and step.run.scope == self.scope
              and step.run.authority == self.authority and step.run.uncertain is False
              and step.dispatch_disposition == 'NOT_SENT' and step.input_ns is None, 'state')
        _need(all(type(v) is int for v in (step.start_ns, step.deadline_ns, step.locate_deadline_ns))
              and self.ports.clock() < min(step.deadline_ns, step.locate_deadline_ns, self.run_deadline_ns), 'deadline')

    # @anchor:wa.learn.mw_live_evidence.LiveEvidenceOwner.validate_policy
    def validate_policy(self, policy, step):
        try:
            self._configuration()
            self._safe(step)
            _need(type(policy) is LocatePolicy and replace(policy, absolute_deadline_ns=self.policy.absolute_deadline_ns) == self.policy
                  and policy.absolute_deadline_ns <= self.policy.absolute_deadline_ns, 'policy_binding')
            _need(self.partial is None and len(self.allocations) < self.policy.max_calls, 'partial')
            return _YES
        except Exception:
            return _NO

    # @anchor:wa.learn.mw_live_evidence.LiveEvidenceOwner.classify
    def classify(self, target, step):
        try:
            self._safe(step)
            fingerprint = _fingerprint(target)
            _need(target['locale'] in self.trust.supported_locales, 'locale')
            action = step.run.workflow['steps'][step.seq - 1]['action']
            _need(action['path'] == 'coordinates' and action['kind'] in ('click', 'right_click', 'drag'), 'action')
            row = self.primary.get(id(step))
            _need(row is not None and row[0] is step and row[1] == fingerprint, 'primary')
            return row[2]
        except Exception:
            return 'terminal'

    def _text(self, text):
        _need(type(text) is str and len(text.encode('utf-8')) <= self.policy.semantic_bytes
              and not any(v in text for v in self.trust.forbidden_substrings), 'privacy')

    def _new_path(self, allocation):
        fd, name = tempfile.mkstemp(prefix='live-', suffix='.png', dir=self.workspace)
        meta = os.fstat(fd)
        os.close(fd)
        allocation.paths.append(Path(name))
        allocation.owned[Path(name)] = (meta.st_dev, meta.st_ino)
        return Path(name)

    # @anchor:wa.learn.mw_live_evidence.LiveEvidenceOwner.acquire
    def acquire(self, target, step, policy, deadline):
        _need(_yes(self.validate_policy(policy, step)), 'policy')
        _need(self.classify(target, step) in ('miss', 'ambiguity', 'invariant'), 'eligibility')
        _need(self.ports.clock() < deadline, 'deadline')
        _need(_yes(self.ports.resources.reserve('live_evidence', self.ports.limits.peak_bytes)), 'reservation')
        allocation = _Allocation(step, _fingerprint(target))
        self.partial = allocation
        try:
            surface = self.surface_slot['surface']
            start = self.ports.clock()
            frame = surface.frame()
            end = self.ports.clock()
            _need(type(frame) is dict and type(frame.get('seq')) is int and frame['seq'] >= 0
                  and (self.sequence is None or frame['seq'] > self.sequence), 'sequence')
            self.sequence = frame['seq']
            window = frame.get('window')
            _need(type(window) is dict and window.get('id') == surface.window and
                  all(type(frame.get(k)) is int and frame[k] > 0 for k in ('width', 'height'))
                  and (window.get('w'), window.get('h')) == (frame['width'], frame['height']), 'capture')
            _need(step.start_ns <= start <= end <= self.ports.clock() < deadline and
                  (step.run.last_input_ns is None or start > step.run.last_input_ns), 'freshness')
            source = Path(frame['path'])
            data, source_identity = _asset(source, source.parent, policy.image_bytes)
            pinned, marks_path = self._new_path(allocation), self._new_path(allocation)
            with pinned.open('wb') as stream:
                stream.write(data)
            d = self.describer.describe(pinned, marks_out=marks_path, ocr_marks=True, deadline_ns=deadline)
            _need(type(d) is DescribeResult and type(d.width) is int and type(d.height) is int
                  and d.width > 0 and d.height > 0 and d.mark_cap_reached is False and type(d.marks) is tuple
                  and 0 < len(d.marks) <= policy.max_marks and d.marks_png == str(marks_path), 'inventory')
            ids = set()
            for m in d.marks:
                _need(type(m) is Mark and all(type(v) is int for v in (m.id, m.x, m.y, m.w, m.h))
                      and m.id >= 0 and m.id not in ids and m.x >= 0 and m.y >= 0 and m.w > 0 and m.h > 0
                      and m.x + m.w <= d.width and m.y + m.h <= d.height, 'geometry')
                ids.add(m.id)
                _need(type(m.ocr_score_promille) is int and 0 <= m.ocr_score_promille <= 1000
                      and m.missing_reason is None, 'ocr')
                self._text(m.ocr_text)
                self._text(m.kind)
                _need(type(m.score_ppm) is int, 'confidence')
            rgb = _png(data, self.config.decode_limits,
                       deadline_ns=time.monotonic_ns() + min(self.config.decode_limits.operation_ns, max(0, deadline - self.ports.clock())))
            digest = hashlib.sha256(rgb.tobytes()).hexdigest()
            _need((d.width, d.height) == (frame['width'], frame['height']) == (rgb.shape[1], rgb.shape[0])
                  and d.frame_sha256 == digest, 'rgb_binding')
            semantic = _fingerprint(target)
            for key in ('rung', 'mark_kind', 'ocr_label', 'locale', 'capability_id'):
                self._text(target[key])
            self._text(semantic)
            images = tuple(_asset(path, self.workspace, policy.image_bytes) for path in allocation.paths)
            _need(images[0][0] == data, 'pinned_mutation')
            # The locator exports only these bounded rows; never serialize the full description.
            metadata = len(semantic.encode()) + sum(len(m.ocr_text.encode()) + len(m.kind.encode()) + 256 for m in d.marks) + 2048
            if type(self.describer) is LiveOCRDescriber:
                metadata += _ocr_metadata(self.config, self.describer._attempts, self.describer._fallback_counts)
            _need(metadata <= self.config.metadata_bytes, 'metadata')
            binding = FrameBinding(self.scope, self.authority, step.run, step, surface, surface.window,
                frame['seq'], (d.width, d.height), (start, end), source_identity,
                hashlib.sha256(data).hexdigest(), digest,
                tuple((identity, hashlib.sha256(raw).hexdigest()) for raw, identity in images))
            obs = ObservationValue(self.scope, self.authority, 'live:' + uuid.uuid4().hex, start, end,
                target['capability_id'], self.trust.capability_revision, 'ram', 'px', binding, True, None)
            export = _call(self.trust.export_check, scope=self.scope, authority=self.authority,
                observation=obs, description=d, semantic_target=semantic, frame_path=pinned,
                marks_path=marks_path, deadline_ns=deadline)
            custody = self._custody(binding, pinned, marks_path, deadline)
            _need(_yes(export) and _yes(custody), 'admission')
            evidence = LocateEvidence(obs, d, pinned, marks_path, binding, semantic, export, custody, allocation)
            allocation.evidence = evidence
            self.allocations[id(evidence)] = allocation
            self.partial = None
            return evidence
        except Exception:
            if _yes(self._cleanup_partial()):
                self.partial = None
            raise ValueError('LIVE_EVIDENCE:acquisition_refused') from None

    def _custody(self, binding, frame, marks, deadline):
        return _call(self.trust.custody_check, scope=self.scope, authority=self.authority,
            binding=binding, frame_path=frame, marks_path=marks, deadline_ns=deadline)

    # @anchor:wa.learn.mw_live_evidence.LiveEvidenceOwner.revalidate
    def revalidate(self, evidence, step, deadline):
        try:
            a = self.allocations.get(id(evidence))
            _need(a is not None and a.evidence is evidence and not a.released and a.step is step
                  and evidence.allocation is a and step.run is self.run, 'registry')
            b = evidence.frame_binding
            _need(evidence.observation is a.evidence.observation and evidence.observation.value is b and b.scope == self.scope and b.authority == self.authority
                  and step.run.scope == self.scope and step.run.authority == self.authority
                  and b.run is step.run and b.step is step and b.surface is self.surface_slot['surface']
                  and b.window == b.surface.window and self.ports.clock() < min(deadline, self.run_deadline_ns), 'binding')
            for path, expected in zip(a.paths, b.image_identities):
                raw, identity = _asset(path, self.workspace, self.policy.image_bytes)
                _need((identity, hashlib.sha256(raw).hexdigest()) == expected, 'image_identity')
            _need(_yes(self._custody(b, *a.paths, deadline)), 'custody')
            _need(_yes(_call(self.trust.export_check, scope=self.scope, authority=self.authority,
                observation=evidence.observation, description=evidence.description, semantic_target=evidence.semantic_target,
                frame_path=evidence.approved_frame_path, marks_path=evidence.approved_marks_path, deadline_ns=deadline)), 'export')
            return _YES
        except Exception:
            return _NO

    def _support(self, target, evidence, mark, point, step, deadline):
        _need(_fingerprint(target) == evidence.allocation.fingerprint and target['locale'] in self.trust.supported_locales, 'target_binding')
        d = evidence.description
        box = target['rel_box_bps']
        matches = []
        for m in d.marks:
            coords = (m.x * 10000 // d.width, m.y * 10000 // d.height,
                      (m.x + m.w) * 10000 // d.width, (m.y + m.h) * 10000 // d.height)
            if (m.kind == target['mark_kind'] and m.ocr_text == target['ocr_label']
                and box[0] <= coords[0] < coords[2] <= box[2] and box[1] <= coords[1] < coords[3] <= box[3]):
                matches.append(m)
        _need(len(matches) == 1 and matches[0] is mark and mark.ocr_score_promille is not None
              and mark.missing_reason is None and point == (mark.x + mark.w // 2, mark.y + mark.h // 2), 'containment')
        answer = _call(self.trust.support_check, target=target, observation=evidence.observation,
            description=d, mark=mark, point=point, step=step, scope=self.scope, authority=self.authority,
            destination=self.trust.destination, deadline_ns=deadline)
        _need(_yes(answer), 'support')
        return answer

    # @anchor:wa.learn.mw_live_evidence.LiveEvidenceOwner.ground
    def ground(self, target, evidence, mark, point, step, deadline):
        _need(_yes(self.revalidate(evidence, step, deadline)), 'revalidation')
        answer = self._support(target, evidence, mark, point, step, deadline)
        expiry = min(deadline, step.deadline_ns, step.locate_deadline_ns, self.run_deadline_ns, self.policy.absolute_deadline_ns)
        value = GroundedTarget(point, self.trust.destination, evidence.observation, self.scope, self.authority,
                               answer, self.capability_owner, expiry, (0, 0))
        evidence.allocation.grounded[id(value)] = (value, mark)
        return value

    # @anchor:wa.learn.mw_live_evidence.LiveEvidenceOwner.validate_target
    def validate_target(self, target, grounded, evidence, step):
        try:
            _need(_yes(self.revalidate(evidence, step, grounded.expires_ns)), 'revalidation')
            row = evidence.allocation.grounded.get(id(grounded))
            _need(row is not None and row[0] is grounded and grounded.support_owner is self.capability_owner
                  and grounded.observation is evidence.observation, 'grounded_identity')
            self._support(target, evidence, row[1], grounded.point, step, grounded.expires_ns)
            return _YES
        except Exception:
            return _NO

    def _delete(self, allocation):
        try:
            drain = getattr(self.describer, 'drain', None)
            if callable(drain) and not _yes(drain()):
                return _NO
            for path in tuple(allocation.paths):
                _ram_path(self.workspace, self.workspace)
                _need(path.parent == self.workspace and not path.is_symlink(), 'delete_custody')
                if path.exists():
                    meta = path.lstat()
                    _need((meta.st_dev, meta.st_ino) == allocation.owned[path], 'delete_identity')
                    path.unlink()
                allocation.paths.remove(path)
            allocation.released = True
            return _YES
        except Exception:
            return _NO

    def _cleanup_partial(self):
        return _YES if self.partial is None else self._delete(self.partial)

    # @anchor:wa.learn.mw_live_evidence.LiveEvidenceOwner.release
    def release(self, evidence, deadline):
        try:
            a = self.allocations.get(id(evidence))
            if a is None or a.evidence is not evidence or self.ports.clock() >= deadline:
                return _NO
            return _YES if a.released else self._delete(a)
        except Exception:
            return _NO


class _Capabilities:
    def __init__(self, original, owner, accounting):
        self.original, self.owner, self.accounting = original, owner, accounting
        self.ordinary = {}

    def __getattr__(self, name):
        return getattr(self.original, name)

    # @anchor:wa.learn.mw_live_evidence._Capabilities.target
    def target(self, target, state, deadline):
        if self.accounting.run is None:
            self.accounting.run = state.run
        _need(self.accounting.run is state.run, 'accounting_run')
        # MWL2F binds accounting before primary location. Evidence custody must
        # bind independently even when that runner hook has already executed.
        if self.owner.run is None:
            self.owner.run = state.run
        _need(self.owner.run is state.run, 'evidence_run')
        # Ordinary lower-rung successes need no T2 fingerprint.
        fingerprint = None
        try:
            fingerprint = _fingerprint(target)
        except ValueError:
            pass
        try:
            value = self.original.target(target, state, deadline)
        except LocationFailure as exc:
            outcome = {'LOW_CONFIDENCE': 'miss', 'AMBIGUOUS_TARGET': 'ambiguity',
                       'LAYOUT_INVARIANT': 'invariant'}[exc.code]
            self.owner.primary[id(state)] = (state, fingerprint, outcome)
            raise
        except Exception:
            self.owner.primary[id(state)] = (state, fingerprint, 'terminal')
            raise
        self.owner.primary[id(state)] = (state, fingerprint, 'miss' if value is None else 'success')
        if value is None:
            return None
        _need(type(value) is GroundedTarget and value.support_owner is self.original
              and _yes(self.original.validate_target(target, value, state)), 'ordinary_support')
        wrapped = replace(value, support_owner=self)
        _need(len(self.ordinary) < self.owner.ports.limits.acquisitions, 'ordinary_bound')
        self.ordinary[id(wrapped)] = (wrapped, value, state, json.dumps(target, sort_keys=True))
        return wrapped

    # @anchor:wa.learn.mw_live_evidence._Capabilities.validate_target
    def validate_target(self, target, value, state):
        # Exact identity is required. The frozen outer adapter's replace() loses
        # this identity on later validation; refuse rather than authenticate a copy.
        for wrapped, original, step, fingerprint in self.ordinary.values():
            if step is state and fingerprint == json.dumps(target, sort_keys=True) and value is wrapped:
                return self.original.validate_target(target, original, state)
        return _NO


class _Observations:
    def __init__(self, original, owner):
        self.original, self.owner = original, owner

    def __getattr__(self, name):
        return getattr(self.original, name)

    # @anchor:wa.learn.mw_live_evidence._Observations.validate
    def validate(self, value):
        for a in self.owner.allocations.values():
            if a.evidence.observation is value:
                return self.owner.revalidate(a.evidence, a.step, self.owner.run_deadline_ns)
            if a.evidence.observation.publication == getattr(value, 'publication', None):
                return _NO
        return self.original.validate(value)


class _Transport:
    def __init__(self, original, owner):
        self.original, self.owner = original, owner

    def __getattr__(self, name):
        return getattr(self.original, name)

    # @anchor:wa.learn.mw_live_evidence._Transport.cancel_and_drain
    def cancel_and_drain(self, policy, clock):
        if not _yes(self.original.cancel_and_drain(policy, clock)):
            return _NO
        result = self.owner._cleanup_partial()
        if _yes(result):
            self.owner.partial = None
        return result


# @anchor:wa.learn.mw_live_evidence.run_live_workflow
def run_live_workflow(invocation, *, bundle, ports, locator_policy, evidence_policy, trust,
                      workspace, executable, ve_root=None, surface_factory=SrfIdentitySurface,
                      describer=None, locate_transport=None):
    accounting = LocateAccounting(run=None)
    slot = {}
    # Configuration precedes native/model construction and announcement.
    owner = LiveEvidenceOwner(ports=ports, locator_policy=locator_policy, evidence_policy=evidence_policy,
        trust=trust, workspace=workspace, surface_slot=slot, describer=describer)
    if describer is None:
        owner.describer = LiveOCRDescriber(evidence_policy=evidence_policy, locator_policy=locator_policy,
                                           clock=ports.clock, ve_root=ve_root)
    caps = _Capabilities(ports.capabilities, owner, accounting)
    owner.capability_owner = caps
    adapted = replace(ports, capabilities=caps, observations=_Observations(ports.observations, owner))
    transport = _Transport(locate_transport if locate_transport is not None else
                           MuseLocateTransport(workspace, executable=executable), owner)
    def factory(**kwargs):
        if surface_factory is SrfIdentitySurface:
            kwargs['ram_dir'] = str(owner.workspace)
        surface = surface_factory(**kwargs)
        slot['surface'] = surface
        return surface
    with open_srf_owners_with_locator(invocation, ports=adapted, locator_policy=locator_policy,
        evidence_owner=owner, locate_transport=transport, accounting=accounting, surface_factory=factory) as owners:
        result = run_workflow(invocation.workflow, bundle=bundle, owners=owners, limits=ports.limits)
    _need(owner.partial is None and all(a.released for a in owner.allocations.values()), 'close')
    return LiveWorkflowResult(result, accounting.snapshot(),
        ocr_execution=owner.describer.execution_summary() if type(owner.describer) is LiveOCRDescriber else None)
