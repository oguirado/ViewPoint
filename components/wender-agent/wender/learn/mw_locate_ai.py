# @module {"id":"wa.learn.mw_locate_ai","layer":4,"owner":"LEARN","deps":["wa.learn.mw_workflow","wa.perceive.vision_bridge","wa.learn.model_client","wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Item-local pre-dispatch recovery; NOT installed in the frozen workflow runner.

Trusted evidence_owner contract (new, not LivePorts):
  validate_policy(policy, step) -> AdmissionCheck checks the run resource envelope;
  classify(target, step) -> 'miss'/'ambiguity'/'invariant' or a terminal classification;
  acquire(target, step, policy, deadline) -> LocateEvidence, reserving all resources;
  revalidate(evidence, step, deadline) -> AdmissionCheck authenticates current
    publication, original-frame geometry binding, registration and private RAM;
  ground(target, evidence, mark, point, step, deadline) -> GroundedTarget;
  validate_target(target, grounded, evidence, step) -> AdmissionCheck validates
    independent disambiguation, destination, translation and installed ownership;
  release(evidence, deadline) -> AdmissionCheck, retaining ownership on failure.
Owner exposes scope, authority, capability_owner and run_deadline_ns. The allocation
  pins immutable approved images and geometry until release; private paths alone
  are not image/export authorization. Acquisition failures must
retain/reclaim their own partial allocations. No owner approval is manufactured.

Transport contract: generate(payload, evidence, policy, deadline, clock) returns
MuseOutput; cancel_and_drain(policy, clock) returns exact AdmissionCheck. Failed
drain poisons accounting and retains evidence. Callers must keep those owners
alive until explicitly reconciled. No input dispatch or verifier is implemented.
All numeric bounds are mandatory caller configuration. Tokens use input+output
only (cached/reasoning are overlapping subsets); any missing usage yields null.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import tempfile
import threading
import uuid

from wender.learn.model_client import MuseOutput, parse_muse_output
from wender.learn.mw_workflow import GroundedTarget, ObservationValue
from wender.perceive.vision_bridge import DescribeResult, Mark
from wender.scripts.types import AdmissionCheck, Reason, Truth

_SCHEMA = {'type': 'object', 'properties': {'mark_id': {'type': ['integer', 'null']}},
           'required': ['mark_id'], 'additionalProperties': False}


# @anchor:wa.learn.mw_locate_ai.LocatePolicy
@dataclass(frozen=True)
class LocatePolicy:
    max_calls: int
    max_marks: int
    semantic_bytes: int
    request_bytes: int
    response_bytes: int
    stderr_bytes: int
    image_bytes: int
    peak_bytes: int
    retained_bytes: int
    call_ns: int
    poll_ns: int
    cancel_ns: int
    drain_ns: int
    absolute_deadline_ns: int


# @anchor:wa.learn.mw_locate_ai.LocateEvidence
@dataclass(frozen=True)
class LocateEvidence:
    observation: ObservationValue
    description: DescribeResult
    approved_frame_path: object
    approved_marks_path: object
    frame_binding: object
    semantic_target: str
    export_admission: AdmissionCheck
    custody_admission: AdmissionCheck
    allocation: object


# @anchor:wa.learn.mw_locate_ai.LocateChoice
@dataclass(frozen=True)
class LocateChoice:
    mark_id: int | None
    reason: Reason | None
    detail: str


# @anchor:wa.learn.mw_locate_ai.LocateAccountingSnapshot
@dataclass(frozen=True)
class LocateAccountingSnapshot:
    model_calls: int
    tokens_used: int | None
    usage_complete: bool


# @anchor:wa.learn.mw_locate_ai.LocateAccounting
@dataclass
class LocateAccounting:
    """Single run owner. Failed cleanup retains evidence and disables reuse."""
    run: object = field(repr=False)
    attempts: int = field(default=0, init=False)
    known_tokens: int = field(default=0, init=False)
    usage_complete: bool = field(default=True, init=False)
    attempted_steps: set = field(default_factory=set, init=False, repr=False)
    poisoned: bool = field(default=False, init=False)
    retained_evidence: object = field(default=None, init=False, repr=False)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False)

    # @anchor:wa.learn.mw_locate_ai.LocateAccounting.snapshot
    def snapshot(self):
        return LocateAccountingSnapshot(self.attempts,
            self.known_tokens if self.usage_complete else None, self.usage_complete)


# @anchor:wa.learn.mw_locate_ai.LocateResult
@dataclass(frozen=True)
class LocateResult:
    choice: LocateChoice
    grounded_target: GroundedTarget | None
    accounting_snapshot: LocateAccountingSnapshot | None


class _Refusal(Exception):
    def __init__(self, detail, reason=Reason.UNKNOWN):
        self.detail, self.reason = detail, reason


def _need(condition, detail, reason=Reason.UNKNOWN):
    if not condition:
        raise _Refusal(detail, reason)


def _admitted(value):
    return type(value) is AdmissionCheck and value.result is Truth.TRUE and value.reason is None


def _time(clock, deadline):
    _need(clock() < deadline, 'deadline', Reason.DEADLINE)


def _policy(policy):
    _need(type(policy) is LocatePolicy and all(type(v) is int and v > 0
          for v in vars(policy).values()), 'configuration', Reason.CAPACITY)
    _need(policy.poll_ns <= policy.call_ns and
          policy.retained_bytes <= policy.peak_bytes and
          2 * policy.image_bytes + policy.request_bytes + 3 * policy.response_bytes +
          policy.stderr_bytes + policy.retained_bytes <= policy.peak_bytes,
          'resource_bounds', Reason.CAPACITY)


def _observation(value, state, owner):
    return (type(value) is ObservationValue and value.complete is True and
            value.missing_reason is None and type(value.publication) is str and bool(value.publication) and
            value.scope == owner.scope and value.authority == owner.authority and
            type(value.capture_start_ns) is int and type(value.capture_end_ns) is int and
            type(state.start_ns) is int and state.start_ns <= value.capture_start_ns <= value.capture_end_ns and
            (state.run.last_input_ns is None or
             (type(state.run.last_input_ns) is int and value.capture_start_ns > state.run.last_input_ns)))


def _ram_path(path, root, limit=None):
    """Verify canonical containment, private ownership and actual mount type."""
    path, root = Path(path), Path(root)
    resolved, base = path.resolve(strict=True), root.resolve(strict=True)
    _need(path.is_absolute() and root.is_absolute() and path == resolved and root == base and
          resolved.is_relative_to(base), 'ram_containment', Reason.PRIVACY)
    for item in (base, resolved):
        meta = item.stat()
        _need(meta.st_uid == os.getuid() and not meta.st_mode & 0o077,
              'ram_ownership', Reason.PRIVACY)
    mounts = []
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        for line in stream:
            left, right = line.rstrip().split(' - ', 1)
            mount = left.split()[4]
            for code, char in (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
                mount = mount.replace(code, char)
            if resolved.is_relative_to(Path(mount)):
                mounts.append((len(Path(mount).parts), right.split()[0]))
    _need(bool(mounts) and max(mounts)[1] in ('tmpfs', 'ramfs'), 'ram_filesystem', Reason.PRIVACY)
    if limit is not None:
        meta = resolved.stat()
        _need(stat.S_ISREG(meta.st_mode) and meta.st_size <= limit, 'image_bound', Reason.CAPACITY)
    return resolved


def _inventory(evidence, policy, state, owner, deadline, clock):
    _need(type(evidence) is LocateEvidence and _admitted(evidence.export_admission) and
          _admitted(evidence.custody_admission) and evidence.allocation is not None and
          evidence.frame_binding is not None,
          'approved_evidence', Reason.PRIVACY)
    _need(_observation(evidence.observation, state, owner) and
          evidence.observation.capture_end_ns <= clock(), 'freshness', Reason.STALE)
    d = evidence.description
    _need(type(d) is DescribeResult and type(d.width) is int and type(d.height) is int and
          d.width > 0 and d.height > 0 and d.mark_cap_reached is False and
          type(d.marks) is tuple and 0 < len(d.marks) <= policy.max_marks,
          'inventory', Reason.CAPACITY)
    ids = set()
    for m in d.marks:
        _need(type(m) is Mark and all(type(n) is int for n in (m.id, m.x, m.y, m.w, m.h)) and
              m.id >= 0 and m.id not in ids and m.x >= 0 and m.y >= 0 and m.w > 0 and m.h > 0 and
              m.x + m.w <= d.width and m.y + m.h <= d.height, 'geometry', Reason.SCHEMA)
        ids.add(m.id)
    _need(type(evidence.semantic_target) is str and bool(evidence.semantic_target.strip()) and
          len(evidence.semantic_target.encode('utf-8')) <= policy.semantic_bytes,
          'semantic', Reason.PRIVACY)
    _need(_admitted(owner.revalidate(evidence, state, deadline)), 'publication', Reason.STALE)
    _time(clock, deadline)
    return _payload(evidence)


def _payload(evidence):
    # Only approved projection fields cross the export boundary.
    d = evidence.description
    return {'semantic_target': evidence.semantic_target, 'width': d.width, 'height': d.height,
            'marks': [{'id': m.id, 'x': m.x, 'y': m.y, 'w': m.w, 'h': m.h,
                       'kind': m.kind, 'ocr_text': m.ocr_text, 'score_ppm': m.score_ppm}
                      for m in d.marks]}


# @anchor:wa.learn.mw_locate_ai.locate_once
def locate_once(target, *, step_state, policy, evidence_owner, transport, accounting, clock):
    """Return a validated proposal or sanitized refusal; never send input."""
    if not accounting._lock.acquire(blocking=False):
        return LocateResult(LocateChoice(None, Reason.CONFLICT, 'concurrent'), None, None)
    evidence = None
    deadline = None
    choice = LocateChoice(None, Reason.UNKNOWN, 'unavailable')
    grounded = None
    try:
        _policy(policy)
        state = step_state
        _need(accounting.run is state.run and not accounting.poisoned, 'account_owner', Reason.FOREIGN)
        _need(state.run.uncertain is False and state.dispatch_disposition == 'NOT_SENT' and
              state.input_ns is None and state.run.current_step is state,
              'predispatch', Reason.CONFLICT)
        _need(all(type(n) is int for n in (state.deadline_ns, state.locate_deadline_ns,
                                         evidence_owner.run_deadline_ns)), 'deadlines', Reason.DEADLINE)
        deadline = min(state.deadline_ns, state.locate_deadline_ns,
                       evidence_owner.run_deadline_ns, policy.absolute_deadline_ns, clock() + policy.call_ns)
        _time(clock, deadline)
        _need(evidence_owner.classify(target, state) in ('miss', 'ambiguity', 'invariant'),
              'ineligible', Reason.CONFLICT)
        key = state.seq
        _need(type(key) is int and key >= 0 and accounting.attempts < policy.max_calls and
              key not in accounting.attempted_steps, 'budget', Reason.CAPACITY)
        _need(_admitted(evidence_owner.validate_policy(policy, state)), 'envelope', Reason.CAPACITY)
        evidence = evidence_owner.acquire(target, state, policy, deadline)
        payload = _inventory(evidence, policy, state, evidence_owner, deadline, clock)
        _need(len(json.dumps(payload, ensure_ascii=False).encode()) <= policy.request_bytes,
              'request_bound', Reason.CAPACITY)
        _time(clock, deadline)
        accounting.attempts += 1
        accounting.attempted_steps.add(key)
        previous_complete = accounting.usage_complete
        accounting.usage_complete = False  # launch failures and refusals still count
        output = transport.generate(payload, evidence, policy, deadline, clock)
        _time(clock, deadline)
        _need(type(output) is MuseOutput and output.extra_objects == 0 and set(output) == {'mark_id'} and
              (output['mark_id'] is None or type(output['mark_id']) is int), 'answer', Reason.SCHEMA)
        usage = output.usage
        known = (type(usage) is dict and all(type(usage.get(k)) is int and usage[k] >= 0
                                          for k in ('input_tokens', 'output_tokens')))
        # Previous unknown attempts cannot be repaired by later measurements.
        accounting.usage_complete = previous_complete and known
        if known:
            accounting.known_tokens += usage['input_tokens'] + usage['output_tokens']
        _need(output['mark_id'] is not None, 'refused')
        marks = {m.id: m for m in evidence.description.marks}
        _need(output['mark_id'] in marks, 'membership', Reason.FOREIGN)
        _need(_admitted(evidence_owner.revalidate(evidence, state, deadline)), 'publication', Reason.STALE)
        _need(_observation(evidence.observation, state, evidence_owner) and
              evidence.observation.capture_end_ns <= clock(), 'freshness', Reason.STALE)
        mark = marks[output['mark_id']]
        point = (mark.x + mark.w // 2, mark.y + mark.h // 2)
        value = evidence_owner.ground(target, evidence, mark, point, state, deadline)
        _need(type(value) is GroundedTarget and value.point == point and value.observation is evidence.observation and
              value.scope == evidence_owner.scope and value.authority == evidence_owner.authority and
              value.support_owner is evidence_owner.capability_owner and _admitted(value.applicability) and
              type(value.expires_ns) is int and clock() < min(value.expires_ns, deadline),
              'grounding', Reason.FOREIGN)
        _need(_admitted(evidence_owner.validate_target(target, value, evidence, state)),
              'independent_support', Reason.CONFLICT)
        _time(clock, deadline)
        grounded, choice = value, LocateChoice(mark.id, None, 'located')
    except _Refusal as exc:
        choice = LocateChoice(None, exc.reason, exc.detail)
    except Exception:
        choice = LocateChoice(None, Reason.UNKNOWN, 'dependency_failure')
    finally:
        if evidence is not None:
            try:
                cleanup_start = clock()
                drained = _admitted(transport.cancel_and_drain(policy, clock))
                _need(drained and clock() <= cleanup_start + policy.cancel_ns + policy.drain_ns,
                      'drain', Reason.CAPACITY)
                release_deadline = clock() + policy.drain_ns
                released = _admitted(evidence_owner.release(evidence, release_deadline))
                _need(released and clock() <= release_deadline, 'release', Reason.CAPACITY)
            except Exception:
                accounting.poisoned = True
                accounting.retained_evidence = evidence
                grounded, choice = None, LocateChoice(None, Reason.CAPACITY, 'cleanup')
        snapshot = accounting.snapshot()
        accounting._lock.release()
    return LocateResult(choice, grounded, snapshot)


# @anchor:wa.learn.mw_locate_ai.MuseLocateTransport
class MuseLocateTransport:
    """Bounded CLI transport. workspace must be an already approved private RAM root."""

    # @anchor:wa.learn.mw_locate_ai.MuseLocateTransport.__init__
    def __init__(self, workspace, *, executable):
        self.workspace = _ram_path(workspace, workspace)
        self.executable = str(executable)
        self._process = None
        self._directory = None
        self._lock = threading.Lock()

    # @anchor:wa.learn.mw_locate_ai.MuseLocateTransport.generate
    def generate(self, payload, evidence, policy, deadline, clock):
        _need(self._lock.acquire(blocking=False), 'transport_busy', Reason.CONFLICT)
        try:
            _policy(policy)
            _need(self._process is None and self._directory is None, 'transport_retained', Reason.CONFLICT)
            _need(type(evidence) is LocateEvidence and _admitted(evidence.export_admission) and
                  _admitted(evidence.custody_admission) and evidence.frame_binding is not None and
                  payload == _payload(evidence), 'transport_export', Reason.PRIVACY)
            images = [_ram_path(p, self.workspace, policy.image_bytes)
                      for p in (evidence.approved_frame_path, evidence.approved_marks_path)]
            self._directory = tempfile.TemporaryDirectory(prefix='locate-', dir=self.workspace)
            directory = Path(self._directory.name)
            prompt, schema = directory / 'prompt.txt', directory / 'schema.json'
            encoded = json.dumps(payload, ensure_ascii=False).encode()
            _need(len(encoded) <= policy.request_bytes, 'request_bound', Reason.CAPACITY)
            prompt.write_bytes(encoded)
            schema.write_text(json.dumps(_SCHEMA), encoding='utf-8')
            prompt.chmod(0o600)
            schema.chmod(0o600)
            argv = [self.executable, 'exec', '--json', '--prompt-file', str(prompt)]
            for path in images:
                argv.extend(['--image', str(path)])
            argv.extend(['--output-schema', str(schema), '--model', 'muse-spark-1.3',
                '--reasoning-effort', 'medium', '--workspace', str(self.workspace),
                '--trust-workspace', '--sandbox-network', 'restricted', '--approval-mode', 'never',
                '--contributor-mode', 'on', '--disable-write', '--disable-web-tools',
                '--disable-reminders', '--max-model-steps', '1', '--session-id', str(uuid.uuid4())])
            _time(clock, deadline)
            self._process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, close_fds=True)
            buffers = [bytearray(), bytearray()]
            with selectors.DefaultSelector() as selector:
                for index, pipe in enumerate((self._process.stdout, self._process.stderr)):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, index)
                while selector.get_map():
                    _time(clock, deadline)
                    for key, _ in selector.select(min(policy.poll_ns, max(0, deadline-clock())) / 1e9):
                        index = key.data
                        limit = policy.response_bytes if index == 0 else policy.stderr_bytes
                        chunk = os.read(key.fileobj.fileno(), min(65536, limit-len(buffers[index])+1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            _need(len(buffers[index]) + len(chunk) <= limit, 'output_bound', Reason.CAPACITY)
                            buffers[index].extend(chunk)
            _time(clock, deadline)
            self._process.wait(timeout=max(0, deadline-clock()) / 1e9)
            _need(self._process.returncode == 0, 'backend')
            return parse_muse_output(buffers[0].decode('utf-8', errors='strict'))
        finally:
            self._lock.release()

    # @anchor:wa.learn.mw_locate_ai.MuseLocateTransport.cancel_and_drain
    def cancel_and_drain(self, policy, clock):
        if not self._lock.acquire(blocking=False):
            return AdmissionCheck(Truth.FALSE, Reason.CONFLICT)
        try:
            process = self._process
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=policy.cancel_ns / 1e9)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=policy.drain_ns / 1e9)
                else:
                    process.wait(timeout=policy.drain_ns / 1e9)
                for pipe in (process.stdout, process.stderr):
                    pipe.close()
                self._process = None
            if self._directory is not None:
                self._directory.cleanup()
                self._directory = None
            return AdmissionCheck(Truth.TRUE, None)
        except Exception:
            return AdmissionCheck(Truth.FALSE, Reason.CAPACITY)
        finally:
            self._lock.release()
